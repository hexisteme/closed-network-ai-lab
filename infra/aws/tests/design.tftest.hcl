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
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ap-northeast-2:111122223333:unexpected" }
  }
}

override_resource {
  target = aws_sns_topic.alarms
  values = { arn = "arn:aws:sns:ap-northeast-2:111122223333:alarms" }
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
  target = aws_cloudwatch_log_group.lab["dns"]
  values = { arn = "arn:aws:logs:ap-northeast-2:111122223333:log-group:dns" }
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
          Sid       = "WriteItsOwnLogsThroughTheEndpoint"
          Effect    = "Allow"
          Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource  = "${var.agent_logs}:*"
          Condition = { StringEquals = { "aws:SourceVpce" = aws_vpc_endpoint.logs.id } }
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
    error_message = "The agent role grants four things: the listed models through the Bedrock endpoint, the vector store as rag, its own logs through the logs endpoint, and the lab key through CloudWatch Logs."
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
    condition = ({ for p in aws_rds_cluster_parameter_group.lab.parameter : p.name => [p.value, p.apply_method] if contains(
      ["shared_preload_libraries", "pgaudit.log", "log_disconnections"], p.name) } == {
      shared_preload_libraries = ["pg_stat_statements,pgaudit", "pending-reboot"]
      "pgaudit.log"            = ["ddl,role", "immediate"]
      log_disconnections       = ["1", "immediate"]
    })
    error_message = "DDL and role changes must be audited by pgAudit, loaded next to pg_stat_statements rather than instead of it, and sessions must log their end."
  }

  assert {
    condition = (toset([for g in aws_cloudwatch_log_group.lab : g.name]) == toset([
      "/closed-network-ai-lab/vpc-flow", "/closed-network-ai-lab/agent", "/closed-network-ai-lab/bedrock-invocations",
      "/closed-network-ai-lab/resolver-queries", "/aws/rds/cluster/closed-network-ai-lab/postgresql",
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
                "arn:aws:logs:ap-northeast-2:111122223333:log-group:/closed-network-ai-lab/resolver-queries",
                "arn:aws:logs:ap-northeast-2:111122223333:log-group:/closed-network-ai-lab/vpc-flow",
                "arn:aws:logs:ap-northeast-2:111122223333:log-group:/closed-network-ai-lab/bedrock-invocations",
                "arn:aws:logs:ap-northeast-2:111122223333:log-group:/aws/rds/cluster/closed-network-ai-lab/postgresql",
              ]
            }
          }
        },
        {
          Sid       = "AlarmsOfThisLabPublishToTheEncryptedTopic"
          Effect    = "Allow"
          Principal = { Service = "cloudwatch.amazonaws.com" }
          Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
          Resource  = "*"
          Condition = {
            StringEquals = { "aws:SourceAccount" = "111122223333" }
            ArnEquals = { "aws:SourceArn" = [
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-client-errors",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-log-delivery-failed",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-server-errors",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-throttles",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-dns-refused-names",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-db-capacity-0",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-db-capacity-1",
            ] }
          }
        },
      ]
    }
    error_message = "The lab key must rotate, and only the account, CloudWatch Logs for these five groups, and this lab's alarms may use it."
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
    condition = (aws_default_network_acl.lab.subnet_ids == toset(concat(aws_subnet.app[*].id, aws_subnet.data[*].id))
      && length(aws_default_network_acl.lab.ingress) > 0 && length(aws_default_network_acl.lab.egress) > 0
      && alltrue([for r in concat(tolist(aws_default_network_acl.lab.ingress), tolist(aws_default_network_acl.lab.egress)) :
    r.cidr_block == var.vpc_cidr && r.action == "allow" && !contains(["-1", "all"], r.protocol)]))
    error_message = "The subnets' network ACL must admit only the VPC's own range, port by port."
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

run "problems_raise_an_alarm" {
  command = apply

  variables {
    alarm_emails = ["oncall@example.com"]
  }

  assert {
    condition = (aws_sns_topic.alarms.kms_master_key_id == "arn:aws:kms:ap-northeast-2:111122223333:key/lab"
    && [for s in values(aws_sns_topic_subscription.alarms) : [s.protocol, s.endpoint, s.topic_arn]] == [["email", "oncall@example.com", "arn:aws:sns:ap-northeast-2:111122223333:alarms"]])
    error_message = "The alarm topic must be encrypted with the lab key and deliver to the given addresses."
  }

  assert {
    condition = jsondecode(aws_sns_topic_policy.alarms.policy) == {
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "AlarmsOfThisLabPublish"
          Effect    = "Allow"
          Principal = { Service = "cloudwatch.amazonaws.com" }
          Action    = "sns:Publish"
          Resource  = "arn:aws:sns:ap-northeast-2:111122223333:alarms"
          Condition = {
            StringEquals = { "aws:SourceAccount" = "111122223333" }
            ArnEquals = { "aws:SourceArn" = [
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-client-errors",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-log-delivery-failed",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-server-errors",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-throttles",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-dns-refused-names",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-db-capacity-0",
              "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-db-capacity-1",
            ] }
          }
        },
        {
          Sid       = "NothingElsePublishes"
          Effect    = "Deny"
          Principal = "*"
          Action    = "sns:Publish"
          Resource  = "arn:aws:sns:ap-northeast-2:111122223333:alarms"
          Condition = { ArnNotEquals = { "aws:SourceArn" = [
            "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-client-errors",
            "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-log-delivery-failed",
            "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-server-errors",
            "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-bedrock-throttles",
            "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-dns-refused-names",
            "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-db-capacity-0",
            "arn:aws:cloudwatch:ap-northeast-2:111122223333:alarm:closed-network-ai-lab-db-capacity-1",
          ] } }
        },
      ]
    }
    error_message = "Only the seven alarms of this lab, named one by one, may publish to the topic; anyone else is denied, identities in this account included."
  }

  assert {
    condition = alltrue([for a in concat(values(aws_cloudwatch_metric_alarm.bedrock), [aws_cloudwatch_metric_alarm.dns_refused_names], aws_cloudwatch_metric_alarm.db_capacity) :
    a.actions_enabled && a.alarm_actions == toset(["arn:aws:sns:ap-northeast-2:111122223333:alarms"]) && a.ok_actions == toset(["arn:aws:sns:ap-northeast-2:111122223333:alarms"])])
    error_message = "Every alarm must notify the topic when it fires and when it clears."
  }

  assert {
    condition = sort(concat([for a in values(aws_cloudwatch_metric_alarm.bedrock) : a.alarm_name], [aws_cloudwatch_metric_alarm.dns_refused_names.alarm_name], aws_cloudwatch_metric_alarm.db_capacity[*].alarm_name)) == tolist([
      "closed-network-ai-lab-bedrock-client-errors",
      "closed-network-ai-lab-bedrock-log-delivery-failed",
      "closed-network-ai-lab-bedrock-server-errors",
      "closed-network-ai-lab-bedrock-throttles",
      "closed-network-ai-lab-db-capacity-0",
      "closed-network-ai-lab-db-capacity-1",
      "closed-network-ai-lab-dns-refused-names",
    ])
    error_message = "The alarms must be exactly the seven the topic and key policies name."
  }

  assert {
    condition = (jsonencode({ for k, a in aws_cloudwatch_metric_alarm.bedrock : k => [a.namespace, a.metric_name, a.statistic, a.period, a.evaluation_periods, a.comparison_operator, a.threshold, a.treat_missing_data] }) == jsonencode({
      throttles           = ["AWS/Bedrock", "InvocationThrottles", "Sum", 300, 1, "GreaterThanThreshold", 0, "notBreaching"]
      server_errors       = ["AWS/Bedrock", "InvocationServerErrors", "Sum", 300, 1, "GreaterThanThreshold", 0, "notBreaching"]
      client_errors       = ["AWS/Bedrock", "InvocationClientErrors", "Sum", 300, 1, "GreaterThanThreshold", 0, "notBreaching"]
      log_delivery_failed = ["AWS/Bedrock", "ModelInvocationLogsCloudWatchDeliveryFailure", "Sum", 300, 1, "GreaterThanThreshold", 0, "notBreaching"]
    }) && alltrue([for a in values(aws_cloudwatch_metric_alarm.bedrock) : try(length(a.dimensions), 0) == 0]))
    error_message = "Bedrock throttles, server errors, client errors and failed invocation-log deliveries must each raise an alarm, counted across all models: a ModelId filter undercounts errors."
  }

  assert {
    condition = (aws_cloudwatch_metric_alarm.dns_refused_names.namespace == "AWS/Route53Resolver"
      && aws_cloudwatch_metric_alarm.dns_refused_names.metric_name == "FirewallRuleQueryVolume"
      && aws_cloudwatch_metric_alarm.dns_refused_names.statistic == "Sum"
      && aws_cloudwatch_metric_alarm.dns_refused_names.period == 300
      && aws_cloudwatch_metric_alarm.dns_refused_names.evaluation_periods == 1
      && aws_cloudwatch_metric_alarm.dns_refused_names.comparison_operator == "GreaterThanThreshold"
      && aws_cloudwatch_metric_alarm.dns_refused_names.threshold == 0
      && aws_cloudwatch_metric_alarm.dns_refused_names.dimensions == tomap({
        FirewallRuleGroupId  = aws_route53_resolver_firewall_rule_group.lab.id
        FirewallDomainListId = aws_route53_resolver_firewall_domain_list.everything.id
    }))
    error_message = "A query for any name outside the allowed list must raise an alarm."
  }

  assert {
    condition = (length(aws_cloudwatch_metric_alarm.db_capacity) == length(aws_rds_cluster_instance.lab)
      && alltrue([for i, a in aws_cloudwatch_metric_alarm.db_capacity : a.namespace == "AWS/RDS"
        && a.metric_name == "ACUUtilization"
        && a.statistic == "Maximum"
        && a.period == 300
        && a.comparison_operator == "GreaterThanOrEqualToThreshold"
        && a.threshold == 90
        && a.evaluation_periods == 3
        && a.datapoints_to_alarm == 3
        && a.treat_missing_data == "breaching"
    && a.dimensions == tomap({ DBInstanceIdentifier = aws_rds_cluster_instance.lab[i].identifier })]))
    error_message = "Each vector store instance reaching 90% or more of its capacity ceiling in each of three consecutive 5-minute periods, or silent for as long, must raise its own alarm: a cluster average hides one instance at the ceiling."
  }
}

run "an_alarm_address_that_is_not_an_email_is_refused" {
  command = plan

  variables {
    alarm_emails = ["oncall"]
  }

  expect_failures = [var.alarm_emails]
}

run "a_vpc_larger_than_aws_allows_is_refused" {
  command = plan

  variables {
    vpc_cidr = "10.0.0.0/8"
  }

  expect_failures = [var.vpc_cidr]
}

run "refused_names_leave_a_record" {
  command = apply

  assert {
    condition = (aws_route53_resolver_query_log_config.lab.destination_arn == "arn:aws:logs:ap-northeast-2:111122223333:log-group:dns"
      && aws_route53_resolver_query_log_config_association.lab.resource_id == aws_vpc.lab.id
    && aws_route53_resolver_query_log_config_association.lab.resolver_query_log_config_id == aws_route53_resolver_query_log_config.lab.id)
    error_message = "Every DNS query from the VPC, refused ones included, must be logged to the resolver-queries group."
  }

  assert {
    condition = jsondecode(aws_cloudwatch_log_resource_policy.resolver_queries.policy_document) == {
      Version = "2012-10-17"
      Statement = [{
        Sid       = "ResolverDeliversQueryLogs"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource  = "arn:aws:logs:ap-northeast-2:111122223333:log-group:dns:log-stream:*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = "111122223333" }
          ArnLike      = { "aws:SourceArn" = "arn:aws:logs:ap-northeast-2:111122223333:*" }
        }
      }]
    }
    error_message = "Only the log delivery service, acting for this account, may write the query log group."
  }
}
