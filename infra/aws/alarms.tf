# Problems the logs record but nobody would read in time raise an alarm on one topic: Bedrock
# throttling, failing or refusing calls, invocation logs that stopped arriving, a name refused by
# DNS Firewall, and a vector store instance near its capacity ceiling. Every alarm also reports when it
# clears. The topic is encrypted with the lab key, and only this lab's alarms may publish to it:
# within the account an identity policy alone could otherwise grant sns:Publish, so a Deny closes that.

resource "aws_sns_topic" "alarms" {
  name              = "${var.name}-alarms"
  kms_master_key_id = aws_kms_key.lab.arn
}

resource "aws_sns_topic_policy" "alarms" {
  arn = aws_sns_topic.alarms.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AlarmsOfThisLabPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alarms.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
          ArnEquals    = { "aws:SourceArn" = local.alarm_arns }
        }
      },
      {
        Sid       = "NothingElsePublishes"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alarms.arn
        Condition = { ArnNotEquals = { "aws:SourceArn" = local.alarm_arns } }
      },
    ]
  })
}

resource "aws_sns_topic_subscription" "alarms" {
  for_each = toset(var.alarm_emails)

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

# Counted across all models: AWS notes that filtering these metrics by ModelId undercounts errors.
resource "aws_cloudwatch_metric_alarm" "bedrock" {
  for_each = local.bedrock_alarm_metrics

  alarm_name          = "${var.name}-bedrock-${replace(each.key, "_", "-")}"
  alarm_description   = "Bedrock ${each.value} in the last 5 minutes."
  namespace           = "AWS/Bedrock"
  metric_name         = each.value
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# Allowed names match the earlier rule, so a query counted against the catch-all list is a refused one.
resource "aws_cloudwatch_metric_alarm" "dns_refused_names" {
  alarm_name          = "${var.name}-dns-refused-names"
  alarm_description   = "DNS Firewall refused a name outside the allowed list; the resolver query log says which name and which address."
  namespace           = "AWS/Route53Resolver"
  metric_name         = "FirewallRuleQueryVolume"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    FirewallRuleGroupId  = aws_route53_resolver_firewall_rule_group.lab.id
    FirewallDomainListId = aws_route53_resolver_firewall_domain_list.everything.id
  }
}

# One alarm per instance: AWS documents ACUUtilization per instance, and a cluster average would
# hide a writer at its ceiling behind an idle reader. Missing data counts as breaching: an instance
# that stops reporting is no better than a full one.
resource "aws_cloudwatch_metric_alarm" "db_capacity" {
  count = length(aws_rds_cluster_instance.lab)

  alarm_name          = "${var.name}-db-capacity-${count.index}"
  alarm_description   = "A vector store instance reached 90% or more of its ACU ceiling in each of three consecutive 5-minute periods; raise db_max_capacity or find the load."
  namespace           = "AWS/RDS"
  metric_name         = "ACUUtilization"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 90
  treat_missing_data  = "breaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    DBInstanceIdentifier = aws_rds_cluster_instance.lab[count.index].identifier
  }
}
