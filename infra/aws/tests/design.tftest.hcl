# The design's claims as assertions. A mock AWS provider stands in for AWS, so these run offline
# with no credentials and create nothing: terraform test (from infra/aws).
#
# Policies are compared whole, so an added statement fails as surely as a changed one. Every
# role, key and log group the policies name gets its own fixed ARN below, so a policy that names
# the wrong one fails too.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  # The provider checks that these attributes are ARNs. Anything not given its own ARN below gets
  # these, which no assertion expects.
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:ap-northeast-2:111122223333:key/unexpected" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/unexpected" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ap-northeast-2:111122223333:log-group:unexpected" }
  }
}

override_resource {
  target = aws_kms_key.lab
  values = { arn = "arn:aws:kms:ap-northeast-2:111122223333:key/lab" }
}

override_resource {
  target = aws_iam_role.agent
  values = { arn = "arn:aws:iam::111122223333:role/agent" }
}

override_resource {
  target = aws_iam_role.flow_logs
  values = { arn = "arn:aws:iam::111122223333:role/flow-logs" }
}

override_resource {
  target = aws_iam_role.bedrock_logging
  values = { arn = "arn:aws:iam::111122223333:role/bedrock-logging" }
}

override_resource {
  target = aws_rds_cluster.lab
  values = {
    endpoint            = "lab.cluster-mock.ap-northeast-2.rds.amazonaws.com"
    reader_endpoint     = "lab.cluster-ro-mock.ap-northeast-2.rds.amazonaws.com"
    cluster_resource_id = "cluster-MOCK"
  }
}

override_resource {
  target = aws_cloudwatch_log_group.lab["flow"]
  values = { arn = "arn:aws:logs:ap-northeast-2:111122223333:log-group:flow" }
}

override_resource {
  target = aws_cloudwatch_log_group.lab["agent"]
  values = { arn = "arn:aws:logs:ap-northeast-2:111122223333:log-group:agent" }
}

override_resource {
  target = aws_cloudwatch_log_group.lab["invocations"]
  values = { arn = "arn:aws:logs:ap-northeast-2:111122223333:log-group:invocations" }
}

override_resource {
  target = aws_cloudwatch_log_group.lab["postgresql"]
  values = { arn = "arn:aws:logs:ap-northeast-2:111122223333:log-group:postgresql" }
}

variables {
  model_ids = ["amazon.titan-embed-text-v2:0", "anthropic.example-chat-model-v1:0"]
}

run "models_are_reachable_only_as_listed_and_only_through_the_endpoint" {
  command = apply

  variables {
    lab_key        = "arn:aws:kms:ap-northeast-2:111122223333:key/lab"
    agent_role     = "arn:aws:iam::111122223333:role/agent"
    agent_logs     = "arn:aws:logs:ap-northeast-2:111122223333:log-group:agent"
    invoke_actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    listed_models = [
      "arn:aws:bedrock:ap-northeast-2::foundation-model/amazon.titan-embed-text-v2:0",
      "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.example-chat-model-v1:0",
    ]
  }

  assert {
    condition = jsondecode(aws_vpc_endpoint.bedrock_runtime.policy) == {
      Version = "2012-10-17"
      Statement = [{
        Sid       = "AgentInvokesListedModels"
        Effect    = "Allow"
        Principal = { AWS = var.agent_role }
        Action    = var.invoke_actions
        Resource  = var.listed_models
      }]
    }
    error_message = "The Bedrock endpoint must let the agent role invoke the listed models and nothing else."
  }

  assert {
    condition = (aws_vpc_endpoint.bedrock_runtime.service_name == "com.amazonaws.ap-northeast-2.bedrock-runtime"
      && aws_vpc_endpoint.bedrock_runtime.vpc_endpoint_type == "Interface"
      && aws_vpc_endpoint.bedrock_runtime.private_dns_enabled
      && aws_vpc_endpoint.bedrock_runtime.subnet_ids == toset(aws_subnet.app[*].id)
    && aws_vpc_endpoint.bedrock_runtime.security_group_ids == toset([aws_security_group.endpoints.id]))
    error_message = "The Bedrock endpoint must be an interface endpoint with private DNS in the app subnets, behind the endpoint group."
  }

  assert {
    condition = jsondecode(aws_vpc_endpoint.logs.policy) == {
      Version = "2012-10-17"
      Statement = [{
        Sid       = "AgentWritesItsLogGroup"
        Effect    = "Allow"
        Principal = { AWS = var.agent_role }
        Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource  = "${var.agent_logs}:*"
      }]
    }
    error_message = "The logs endpoint must let the agent role write its own log group and nothing else."
  }

  assert {
    condition = (aws_vpc_endpoint.logs.service_name == "com.amazonaws.ap-northeast-2.logs"
      && aws_vpc_endpoint.logs.vpc_endpoint_type == "Interface"
      && aws_vpc_endpoint.logs.private_dns_enabled
    && aws_vpc_endpoint.logs.subnet_ids == toset(aws_subnet.app[*].id))
    error_message = "The logs endpoint must be an interface endpoint with private DNS in the app subnets."
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.agent.policy) == {
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "InvokeListedModelsThroughTheEndpoint"
          Effect    = "Allow"
          Action    = var.invoke_actions
          Resource  = var.listed_models
          Condition = { StringEquals = { "aws:SourceVpce" = aws_vpc_endpoint.bedrock_runtime.id } }
        },
        {
          Sid      = "SignInToTheVectorStoreAsRag"
          Effect   = "Allow"
          Action   = "rds-db:connect"
          Resource = "arn:aws:rds-db:ap-northeast-2:111122223333:dbuser:cluster-MOCK/rag"
        },
        {
          Sid      = "WriteItsOwnLogs"
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "${var.agent_logs}:*"
        },
        {
          Sid       = "UseTheLabKeyOnlyThroughCloudWatchLogs"
          Effect    = "Allow"
          Action    = ["kms:Encrypt", "kms:ReEncrypt*", "kms:Decrypt", "kms:GenerateDataKey*", "kms:Describe*"]
          Resource  = var.lab_key
          Condition = { StringEquals = { "kms:ViaService" = "logs.ap-northeast-2.amazonaws.com" } }
        },
      ]
    }
    error_message = "The agent role grants four things: the listed models through this endpoint, the vector store as rag, its own logs, and the lab key through CloudWatch Logs."
  }

  assert {
    condition = jsondecode(aws_iam_role.agent.assume_role_policy).Statement == [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = "111122223333" } }
    }]
    error_message = "Only ECS tasks in this account may take the agent role."
  }
}

run "nothing_is_public_and_everything_is_encrypted" {
  command = apply

  variables {
    lab_key = "arn:aws:kms:ap-northeast-2:111122223333:key/lab"
  }

  assert {
    condition     = alltrue([for s in concat(aws_subnet.app, aws_subnet.data) : s.map_public_ip_on_launch == false])
    error_message = "No subnet may hand out public addresses."
  }

  assert {
    condition     = alltrue([for i in aws_rds_cluster_instance.lab : i.publicly_accessible == false && i.performance_insights_enabled && i.performance_insights_kms_key_id == var.lab_key])
    error_message = "No database instance may be public, and Performance Insights must be on and use the lab key."
  }

  assert {
    condition = (aws_rds_cluster.lab.storage_encrypted && aws_rds_cluster.lab.kms_key_id == var.lab_key
      && aws_rds_cluster.lab.iam_database_authentication_enabled
      && aws_rds_cluster.lab.manage_master_user_password && aws_rds_cluster.lab.master_user_secret_kms_key_id == var.lab_key
    && aws_rds_cluster.lab.deletion_protection)
    error_message = "The vector store must be encrypted with the lab key, use IAM sign-in, keep its master password in Secrets Manager under the lab key and resist deletion."
  }

  assert {
    condition = (aws_rds_cluster.lab.db_cluster_parameter_group_name == aws_rds_cluster_parameter_group.lab.name
    && one([for p in aws_rds_cluster_parameter_group.lab.parameter : p.value if p.name == "rds.force_ssl"]) == "1")
    error_message = "The cluster must use the parameter group that refuses connections without TLS."
  }

  assert {
    condition = (toset([for g in aws_cloudwatch_log_group.lab : g.name]) == toset([
      "/closed-network-ai-lab/vpc-flow", "/closed-network-ai-lab/agent", "/closed-network-ai-lab/bedrock-invocations",
      "/aws/rds/cluster/closed-network-ai-lab/postgresql",
    ]) && alltrue([for g in aws_cloudwatch_log_group.lab : g.kms_key_id == var.lab_key && g.retention_in_days >= 365]))
    error_message = "Every log group, including the one Aurora exports to, must use the lab key and keep a year or more."
  }

  assert {
    condition = aws_kms_key.lab.enable_key_rotation && jsondecode(aws_kms_key.lab.policy) == {
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "AccountAdministers"
          Effect    = "Allow"
          Principal = { AWS = "arn:aws:iam::111122223333:root" }
          Action    = "kms:*"
          Resource  = "*"
        },
        {
          Sid       = "LogsEncryptTheseGroups"
          Effect    = "Allow"
          Principal = { Service = "logs.ap-northeast-2.amazonaws.com" }
          Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
          Resource  = "*"
          Condition = {
            ArnLike = {
              "kms:EncryptionContext:aws:logs:arn" = [
                "arn:aws:logs:ap-northeast-2:111122223333:log-group:/closed-network-ai-lab/agent",
                "arn:aws:logs:ap-northeast-2:111122223333:log-group:/closed-network-ai-lab/vpc-flow",
                "arn:aws:logs:ap-northeast-2:111122223333:log-group:/closed-network-ai-lab/bedrock-invocations",
                "arn:aws:logs:ap-northeast-2:111122223333:log-group:/aws/rds/cluster/closed-network-ai-lab/postgresql",
              ]
            }
          }
        },
      ]
    }
    error_message = "The lab key must rotate, and only the account and CloudWatch Logs, for these four groups, may use it."
  }
}

run "no_way_out_but_the_endpoints" {
  command = apply

  variables {
    flow_role = "arn:aws:iam::111122223333:role/flow-logs"
    flow_logs = "arn:aws:logs:ap-northeast-2:111122223333:log-group:flow"
    lab_key   = "arn:aws:kms:ap-northeast-2:111122223333:key/lab"
  }

  assert {
    condition = (length(aws_route_table.private.route) == 0
      && toset(concat(aws_route_table_association.app[*].subnet_id, aws_route_table_association.data[*].subnet_id)) == toset(concat(aws_subnet.app[*].id, aws_subnet.data[*].id))
    && alltrue([for a in concat(aws_route_table_association.app, aws_route_table_association.data) : a.route_table_id == aws_route_table.private.id]))
    error_message = "Every subnet must use the route table that holds only the local route."
  }

  assert {
    condition = (aws_route53_resolver_firewall_domain_list.allowed.domains == toset([
      "bedrock-runtime.ap-northeast-2.amazonaws.com", "logs.ap-northeast-2.amazonaws.com",
      "lab.cluster-mock.ap-northeast-2.rds.amazonaws.com", "lab.cluster-ro-mock.ap-northeast-2.rds.amazonaws.com",
    ]) && aws_route53_resolver_firewall_domain_list.everything.domains == toset(["*"]))
    error_message = "DNS must answer only the endpoint and database names."
  }

  assert {
    condition = (aws_route53_resolver_firewall_rule.allow_named.action == "ALLOW"
      && aws_route53_resolver_firewall_rule.allow_named.firewall_domain_list_id == aws_route53_resolver_firewall_domain_list.allowed.id
      && aws_route53_resolver_firewall_rule.block_the_rest.action == "BLOCK"
      && aws_route53_resolver_firewall_rule.block_the_rest.firewall_domain_list_id == aws_route53_resolver_firewall_domain_list.everything.id
      && aws_route53_resolver_firewall_rule.allow_named.priority < aws_route53_resolver_firewall_rule.block_the_rest.priority
      && aws_route53_resolver_firewall_rule.allow_named.firewall_rule_group_id == aws_route53_resolver_firewall_rule_group.lab.id
      && aws_route53_resolver_firewall_rule.block_the_rest.firewall_rule_group_id == aws_route53_resolver_firewall_rule_group.lab.id
      && aws_route53_resolver_firewall_rule_group_association.lab.vpc_id == aws_vpc.lab.id
      && aws_route53_resolver_firewall_rule_group_association.lab.firewall_rule_group_id == aws_route53_resolver_firewall_rule_group.lab.id
      && aws_route53_resolver_firewall_config.lab.resource_id == aws_vpc.lab.id
    && aws_route53_resolver_firewall_config.lab.firewall_fail_open == "DISABLED")
    error_message = "This VPC's DNS Firewall must allow the named list first, block every other name, and fail closed."
  }

  assert {
    condition = (aws_flow_log.lab.vpc_id == aws_vpc.lab.id && aws_flow_log.lab.traffic_type == "ALL"
    && aws_flow_log.lab.log_destination == var.flow_logs && aws_flow_log.lab.iam_role_arn == var.flow_role)
    error_message = "Flow logs must record all traffic of this VPC into the flow log group through the flow log role."
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.flow_logs.policy) == {
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "WriteTheFlowLogGroup"
          Effect   = "Allow"
          Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
          Resource = [var.flow_logs, "${var.flow_logs}:*"]
        },
        {
          Sid      = "FindTheFlowLogGroup"
          Effect   = "Allow"
          Action   = "logs:DescribeLogGroups"
          Resource = "arn:aws:logs:ap-northeast-2:111122223333:log-group:*"
        },
        {
          Sid       = "UseTheLabKeyOnlyThroughCloudWatchLogs"
          Effect    = "Allow"
          Action    = ["kms:Encrypt", "kms:ReEncrypt*", "kms:Decrypt", "kms:GenerateDataKey*", "kms:Describe*"]
          Resource  = var.lab_key
          Condition = { StringEquals = { "kms:ViaService" = "logs.ap-northeast-2.amazonaws.com" } }
        },
      ]
    }
    error_message = "The flow log role may write the flow log group only."
  }
}

run "only_named_flows_between_named_groups" {
  command = apply

  assert {
    condition = (aws_vpc_security_group_egress_rule.app_to_endpoints.security_group_id == aws_security_group.app.id
      && aws_vpc_security_group_egress_rule.app_to_endpoints.referenced_security_group_id == aws_security_group.endpoints.id
      && [aws_vpc_security_group_egress_rule.app_to_endpoints.ip_protocol, aws_vpc_security_group_egress_rule.app_to_endpoints.from_port, aws_vpc_security_group_egress_rule.app_to_endpoints.to_port] == ["tcp", 443, 443]
      && aws_vpc_security_group_egress_rule.app_to_db.security_group_id == aws_security_group.app.id
      && aws_vpc_security_group_egress_rule.app_to_db.referenced_security_group_id == aws_security_group.db.id
    && [aws_vpc_security_group_egress_rule.app_to_db.ip_protocol, aws_vpc_security_group_egress_rule.app_to_db.from_port, aws_vpc_security_group_egress_rule.app_to_db.to_port] == ["tcp", 5432, 5432])
    error_message = "The agent tasks may reach the endpoints on 443 and the database on 5432 only."
  }

  assert {
    condition = (aws_vpc_security_group_ingress_rule.endpoints_from_app.security_group_id == aws_security_group.endpoints.id
      && aws_vpc_security_group_ingress_rule.endpoints_from_app.referenced_security_group_id == aws_security_group.app.id
      && [aws_vpc_security_group_ingress_rule.endpoints_from_app.ip_protocol, aws_vpc_security_group_ingress_rule.endpoints_from_app.from_port, aws_vpc_security_group_ingress_rule.endpoints_from_app.to_port] == ["tcp", 443, 443]
      && aws_vpc_security_group_ingress_rule.db_from_app.security_group_id == aws_security_group.db.id
      && aws_vpc_security_group_ingress_rule.db_from_app.referenced_security_group_id == aws_security_group.app.id
    && [aws_vpc_security_group_ingress_rule.db_from_app.ip_protocol, aws_vpc_security_group_ingress_rule.db_from_app.from_port, aws_vpc_security_group_ingress_rule.db_from_app.to_port] == ["tcp", 5432, 5432])
    error_message = "The endpoints accept the agent tasks on 443 and the database on 5432, and nobody else."
  }

  assert {
    condition = (alltrue([for g in [aws_security_group.app, aws_security_group.endpoints, aws_security_group.db] : g.vpc_id == aws_vpc.lab.id])
      && aws_vpc_endpoint.logs.security_group_ids == toset([aws_security_group.endpoints.id])
    && aws_rds_cluster.lab.vpc_security_group_ids == toset([aws_security_group.db.id]))
    error_message = "The endpoints and the database must sit behind their own groups in this VPC."
  }

  assert {
    condition     = length(aws_default_security_group.lab.ingress) == 0 && length(aws_default_security_group.lab.egress) == 0
    error_message = "The default security group must stay empty."
  }
}

run "invocation_logs_carry_no_bodies" {
  command = apply

  variables {
    lab_key         = "arn:aws:kms:ap-northeast-2:111122223333:key/lab"
    logging_role    = "arn:aws:iam::111122223333:role/bedrock-logging"
    invocation_logs = "arn:aws:logs:ap-northeast-2:111122223333:log-group:invocations"
  }

  assert {
    condition = (!aws_bedrock_model_invocation_logging_configuration.lab.logging_config[0].text_data_delivery_enabled
      && !aws_bedrock_model_invocation_logging_configuration.lab.logging_config[0].image_data_delivery_enabled
      && !aws_bedrock_model_invocation_logging_configuration.lab.logging_config[0].embedding_data_delivery_enabled
    && !aws_bedrock_model_invocation_logging_configuration.lab.logging_config[0].video_data_delivery_enabled)
    error_message = "Invocation logs must not carry prompts, outputs, images, embeddings or video."
  }

  assert {
    condition = (aws_bedrock_model_invocation_logging_configuration.lab.logging_config[0].cloudwatch_config[0].log_group_name == "/closed-network-ai-lab/bedrock-invocations"
    && aws_bedrock_model_invocation_logging_configuration.lab.logging_config[0].cloudwatch_config[0].role_arn == var.logging_role)
    error_message = "Invocation logs must go to the invocations group through the logging role."
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.bedrock_logging.policy) == {
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "WriteTheInvocationLogStream"
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "${var.invocation_logs}:log-stream:aws/bedrock/modelinvocations"
        },
        {
          Sid       = "UseTheLabKeyOnlyThroughCloudWatchLogs"
          Effect    = "Allow"
          Action    = ["kms:Encrypt", "kms:ReEncrypt*", "kms:Decrypt", "kms:GenerateDataKey*", "kms:Describe*"]
          Resource  = var.lab_key
          Condition = { StringEquals = { "kms:ViaService" = "logs.ap-northeast-2.amazonaws.com" } }
        },
      ]
    }
    error_message = "The logging role may write the invocation log stream only."
  }
}

run "an_empty_model_list_is_refused" {
  command = plan

  variables {
    model_ids = []
  }

  expect_failures = [var.model_ids]
}

run "a_vpc_larger_than_aws_allows_is_refused" {
  command = plan

  variables {
    vpc_cidr = "10.0.0.0/8"
  }

  expect_failures = [var.vpc_cidr]
}
