# Interface endpoints are the only way out of the subnets. Each endpoint policy is the lab
# gateway's allowlist: one principal, the listed actions, the listed resources.

resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = aws_vpc.lab.id
  service_name        = "com.amazonaws.${var.region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.app[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  # The lab's model allowlist: only these foundation models, only invocation, only the agent.
  # Converse and ConverseStream are authorized by these two actions.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AgentInvokesListedModels"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.agent.arn }
      Action    = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      Resource  = local.model_arns
    }]
  })

  tags = { Name = "${var.name}-bedrock-runtime" }
}

resource "aws_vpc_endpoint" "logs" {
  vpc_id              = aws_vpc.lab.id
  service_name        = "com.amazonaws.${var.region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.app[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AgentWritesItsLogGroup"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.agent.arn }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.lab["agent"].arn}:*"
    }]
  })

  tags = { Name = "${var.name}-logs" }
}
