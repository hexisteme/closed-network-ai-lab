output "vpc_id" {
  value = aws_vpc.lab.id
}

output "app_subnet_ids" {
  description = "Where the agent tasks run."
  value       = aws_subnet.app[*].id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "bedrock_runtime_endpoint_id" {
  value = aws_vpc_endpoint.bedrock_runtime.id
}

output "agent_role_arn" {
  value = aws_iam_role.agent.arn
}

output "vector_store_endpoint" {
  description = "Aurora writer endpoint; the agent connects as `rag` with an IAM token over TLS."
  value       = aws_rds_cluster.lab.endpoint
}
