# The lab's gateway logs one line per request with sizes and the decision, never the prompt.
# Bedrock's account-wide invocation log is set the same way: every kind of body delivery is
# off. The user guide does not say what a record holds with all of them off; see the README.

resource "aws_iam_role" "bedrock_logging" {
  name = "${var.name}-bedrock-logging"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "bedrock_logging" {
  name = "${var.name}-bedrock-logging"
  role = aws_iam_role.bedrock_logging.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteTheInvocationLogStream"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.lab["invocations"].arn}:log-stream:aws/bedrock/modelinvocations"
      },
      local.write_encrypted_logs,
    ]
  })
}

resource "aws_bedrock_model_invocation_logging_configuration" "lab" {
  logging_config {
    text_data_delivery_enabled      = false
    image_data_delivery_enabled     = false
    embedding_data_delivery_enabled = false
    video_data_delivery_enabled     = false

    cloudwatch_config {
      log_group_name = aws_cloudwatch_log_group.lab["invocations"].name
      role_arn       = aws_iam_role.bedrock_logging.arn
    }
  }

  depends_on = [aws_iam_role_policy.bedrock_logging]
}
