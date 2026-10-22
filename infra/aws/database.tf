# The lab's vector-db as Aurora PostgreSQL Serverless v2 with pgvector: private subnets only,
# encrypted with the lab key, TLS required, and the agent signs in with IAM, not a password.
# The master password is generated and kept by RDS in Secrets Manager, so it is never in this
# code or in the Terraform state.

resource "aws_db_subnet_group" "lab" {
  name       = "${var.name}-data"
  subnet_ids = aws_subnet.data[*].id
}

resource "aws_rds_cluster_parameter_group" "lab" {
  name   = "${var.name}-aurora-pg17"
  family = "aurora-postgresql17"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name         = "log_disconnections"
    value        = "1"
    apply_method = "immediate"
  }

  # pgAudit records DDL and role changes, such as the master user's setup of `rag`. It is added
  # next to pg_stat_statements, which Performance Insights reads SQL statistics from; setting the
  # list to pgaudit alone would drop it. The library loads at startup, so this applies on reboot,
  # and `CREATE EXTENSION pgaudit` still has to run in the database (see README).
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pgaudit"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "pgaudit.log"
    value        = "ddl,role"
    apply_method = "immediate"
  }
}

resource "aws_rds_cluster" "lab" {
  cluster_identifier = var.name
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = var.db_engine_version
  database_name      = "rag"

  master_username                     = "rag_admin"
  manage_master_user_password         = true
  master_user_secret_kms_key_id       = aws_kms_key.lab.arn
  iam_database_authentication_enabled = true

  db_subnet_group_name            = aws_db_subnet_group.lab.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.lab.name

  storage_encrypted = true
  kms_key_id        = aws_kms_key.lab.arn

  backup_retention_period         = 7
  copy_tags_to_snapshot           = true
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-final"
  enabled_cloudwatch_logs_exports = ["postgresql"]
  depends_on                      = [aws_cloudwatch_log_group.lab]

  serverlessv2_scaling_configuration {
    min_capacity = 0.5
    max_capacity = var.db_max_capacity
  }
}

# One instance per Availability Zone: a writer and a reader to fail over to.
resource "aws_rds_cluster_instance" "lab" {
  count = length(var.availability_zones)

  identifier                            = "${var.name}-${count.index}"
  cluster_identifier                    = aws_rds_cluster.lab.id
  engine                                = aws_rds_cluster.lab.engine
  engine_version                        = aws_rds_cluster.lab.engine_version
  instance_class                        = "db.serverless"
  availability_zone                     = var.availability_zones[count.index]
  publicly_accessible                   = false
  auto_minor_version_upgrade            = true
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.lab.arn
  performance_insights_retention_period = 7
}
