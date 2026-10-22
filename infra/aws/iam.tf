# The agent's identity. It runs as an ECS task in the app subnets (the task definition is out of
# scope here); this role is what the endpoint policies and the database trust.
resource "aws_iam_role" "agent" {
  name = "${var.name}-agent"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "agent" {
  name = "${var.name}-agent"
  role = aws_iam_role.agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Only the listed models, and only through this VPC's endpoint: a copied credential
        # cannot invoke them from anywhere else.
        Sid       = "InvokeListedModelsThroughTheEndpoint"
        Effect    = "Allow"
        Action    = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource  = local.model_arns
        Condition = { StringEquals = { "aws:SourceVpce" = aws_vpc_endpoint.bedrock_runtime.id } }
      },
      {
        Sid      = "SignInToTheVectorStoreAsRag"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:${local.partition}:rds-db:${var.region}:${local.account_id}:dbuser:${aws_rds_cluster.lab.cluster_resource_id}/rag"
      },
      {
        # Through the logs endpoint only: a copied credential cannot write the agent's log from
        # outside the VPC and muddy the record it is judged by.
        Sid       = "WriteItsOwnLogsThroughTheEndpoint"
        Effect    = "Allow"
        Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource  = "${aws_cloudwatch_log_group.lab["agent"].arn}:*"
        Condition = { StringEquals = { "aws:SourceVpce" = aws_vpc_endpoint.logs.id } }
      },
      local.write_encrypted_logs,
    ]
  })
}

resource "aws_iam_role" "flow_logs" {
  name = "${var.name}-flow-logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:ec2:${var.region}:${local.account_id}:vpc-flow-log/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "${var.name}-flow-logs"
  role = aws_iam_role.flow_logs.id

  # The VPC user guide's minimum for a flow log role, scoped to the flow log group where the
  # action allows it. DescribeLogGroups cannot be scoped to one group.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteTheFlowLogGroup"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = ["${aws_cloudwatch_log_group.lab["flow"].arn}", "${aws_cloudwatch_log_group.lab["flow"].arn}:*"]
      },
      {
        Sid      = "FindTheFlowLogGroup"
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:*"
      },
      local.write_encrypted_logs,
    ]
  })
}
