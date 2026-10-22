locals {
  partition  = data.aws_partition.current.partition
  account_id = data.aws_caller_identity.current.account_id

  model_arns = [for id in var.model_ids : "arn:${local.partition}:bedrock:${var.region}::foundation-model/${id}"]

  # The postgresql group is the one Aurora exports to. It is created here first so that it gets
  # the lab key and a retention instead of the defaults RDS would give it.
  log_groups = {
    flow        = "/${var.name}/vpc-flow"
    agent       = "/${var.name}/agent"
    invocations = "/${var.name}/bedrock-invocations"
    postgresql  = "/aws/rds/cluster/${var.name}/postgresql"
  }

  # What a role that writes to a log group encrypted with the lab key needs from the key, used
  # only through CloudWatch Logs (CloudWatch Logs user guide, "Set permissions on the calling IAM
  # principal").
  write_encrypted_logs = {
    Sid       = "UseTheLabKeyOnlyThroughCloudWatchLogs"
    Effect    = "Allow"
    Action    = ["kms:Encrypt", "kms:ReEncrypt*", "kms:Decrypt", "kms:GenerateDataKey*", "kms:Describe*"]
    Resource  = aws_kms_key.lab.arn
    Condition = { StringEquals = { "kms:ViaService" = "logs.${var.region}.amazonaws.com" } }
  }
}

# One customer managed key for the logs, the database and its managed master secret.
resource "aws_kms_key" "lab" {
  description             = "${var.name}: logs, database storage, database master secret"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministers"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "LogsEncryptTheseGroups"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = [
              for group in values(local.log_groups) : "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:${group}"
            ]
          }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "lab" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.lab.key_id
}

resource "aws_cloudwatch_log_group" "lab" {
  for_each = local.log_groups

  name              = each.value
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.lab.arn
}
