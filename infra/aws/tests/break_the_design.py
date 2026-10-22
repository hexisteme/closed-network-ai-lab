"""Break the AWS design one way at a time and show that terraform test notices; trivy's verdict on
the same change is recorded beside it. Each case edits a fresh copy of infra/aws, so the original
is never touched.

A case counts as caught only if the broken copy is still valid Terraform, the one test run that
covers the break fails, and every other run passes. terraform test may skip the runs after a
failed one, so a skip is accepted there and nowhere else. trivy counts as catching it only if it ran
and reported a HIGH or CRITICAL misconfiguration.

Run from infra/aws after terraform init: python3 -I tests/break_the_design.py
Set OUT to a file path to keep the per-case results as JSON.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]

ENDPOINT = "models_are_reachable_only_as_listed_and_only_through_the_endpoint"
PUBLIC = "nothing_is_public_and_everything_is_encrypted"
WAY_OUT = "no_way_out_but_the_endpoints"
FLOWS = "only_named_flows_between_named_groups"
INVOCATION_LOGS = "invocation_logs_carry_no_bodies"
EMPTY_MODELS = "an_empty_model_list_is_refused"
ALARMS = "problems_raise_an_alarm"
ALARM_EMAILS = "an_alarm_address_that_is_not_an_email_is_refused"
LARGE_VPC = "a_vpc_larger_than_aws_allows_is_refused"
RESOLVER_LOG = "refused_names_leave_a_record"

DB_FROM_APP = """  description                  = "PostgreSQL from the agent tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.{}.id"""

DEFAULT_GROUP = """resource "aws_default_security_group" "lab" {{
  vpc_id = aws_vpc.lab.id
{}}}"""

OPEN_INGRESS = """  ingress {
    protocol  = "-1"
    self      = true
    from_port = 0
    to_port   = 0
  }
"""

ROUTE_TABLE = """resource "aws_route_table" "private" {
  vpc_id = aws_vpc.lab.id
"""

ROUTE_OUT = """resource "aws_internet_gateway" "out" {
  vpc_id = aws_vpc.lab.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.lab.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.out.id
  }
"""

ENDPOINT_PRINCIPAL = """      Sid       = "AgentInvokesListedModels"
      Effect    = "Allow"
      Principal = {{ AWS = aws_iam_role.{}.arn }}"""

ENDPOINT_STATEMENT_END = """      Resource  = local.model_arns
    }]"""

ENDPOINT_EXTRA_STATEMENT = """      Resource  = local.model_arns
      }, {
      Effect    = "Allow"
      Principal = "*"
      Action    = "bedrock:*"
      Resource  = "*"
    }]"""

# (case, file, text that must occur exactly once, its replacement, the test run that must fail)
CASES = [
    ("database_made_public", "database.tf",
     "publicly_accessible                   = false", "publicly_accessible                   = true", PUBLIC),
    ("database_unencrypted", "database.tf", "storage_encrypted = true", "storage_encrypted = false", PUBLIC),
    ("tls_not_required", "database.tf",
     'name  = "rds.force_ssl"\n    value = "1"', 'name  = "rds.force_ssl"\n    value = "0"', PUBLIC),
    ("tls_parameter_group_detached", "database.tf",
     "  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.lab.name\n", "", PUBLIC),
    ("app_subnet_hands_out_public_ips", "network.tf",
     "cidrsubnet(var.vpc_cidr, 8, count.index)\n  map_public_ip_on_launch = false",
     "cidrsubnet(var.vpc_cidr, 8, count.index)\n  map_public_ip_on_launch = true", PUBLIC),
    ("logs_kept_a_month", "main.tf", "retention_in_days = var.log_retention_days", "retention_in_days = 30", PUBLIC),
    ("exported_db_logs_left_to_rds", "main.tf",
     '    postgresql  = "/aws/rds/cluster/${var.name}/postgresql"\n', "", PUBLIC),
    ("route_out_through_an_internet_gateway", "network.tf", ROUTE_TABLE, ROUTE_OUT, WAY_OUT),
    ("network_acl_open_to_anywhere", "network.tf",
     "    rule_no    = 100\n    protocol   = \"tcp\"\n    action     = \"allow\"\n    cidr_block = var.vpc_cidr\n    from_port  = 443\n    to_port    = 443\n  }\n\n  ingress {",
     "    rule_no    = 100\n    protocol   = \"tcp\"\n    action     = \"allow\"\n    cidr_block = \"0.0.0.0/0\"\n    from_port  = 443\n    to_port    = 443\n  }\n\n  ingress {", WAY_OUT),
    ("dns_answers_every_name", "dns.tf", 'action                  = "BLOCK"', 'action                  = "ALLOW"', WAY_OUT),
    ("dns_answers_any_aws_name", "dns.tf",
     '"logs.${var.region}.amazonaws.com",', '"logs.${var.region}.amazonaws.com",\n    "*.amazonaws.com",', WAY_OUT),
    ("dns_fails_open", "dns.tf", 'firewall_fail_open = "DISABLED"', 'firewall_fail_open = "ENABLED"', WAY_OUT),
    ("flow_log_into_the_agent_group", "network.tf",
     'log_destination      = aws_cloudwatch_log_group.lab["flow"].arn',
     'log_destination      = aws_cloudwatch_log_group.lab["agent"].arn', WAY_OUT),
    ("agent_may_reach_any_address_on_5432", "network.tf",
     "referenced_security_group_id = aws_security_group.db.id", 'cidr_ipv4                    = "0.0.0.0/0"', FLOWS),
    ("db_accepts_the_endpoint_group", "network.tf", DB_FROM_APP.format("app"), DB_FROM_APP.format("endpoints"), FLOWS),
    ("default_group_opened", "network.tf", DEFAULT_GROUP.format(""), DEFAULT_GROUP.format(OPEN_INGRESS), FLOWS),
    ("endpoint_allows_every_model", "endpoints.tf", "Resource  = local.model_arns", 'Resource  = "*"', ENDPOINT),
    ("endpoint_gains_a_statement", "endpoints.tf", ENDPOINT_STATEMENT_END, ENDPOINT_EXTRA_STATEMENT, ENDPOINT),
    ("endpoint_trusts_another_role", "endpoints.tf",
     ENDPOINT_PRINCIPAL.format("agent"), ENDPOINT_PRINCIPAL.format("flow_logs"), ENDPOINT),
    ("agent_may_invoke_any_model", "iam.tf", "Resource  = local.model_arns", 'Resource  = "*"', ENDPOINT),
    ("agent_permission_not_tied_to_endpoint", "iam.tf",
     '        Condition = { StringEquals = { "aws:SourceVpce" = aws_vpc_endpoint.bedrock_runtime.id } }\n', "", ENDPOINT),
    ("invocation_logs_carry_prompts", "logging.tf",
     "text_data_delivery_enabled      = false", "text_data_delivery_enabled      = true", INVOCATION_LOGS),
    ("logging_role_writes_any_stream", "logging.tf",
     ':log-stream:aws/bedrock/modelinvocations"', ':*"', INVOCATION_LOGS),
    ("key_open_to_any_log_group", "main.tf",
     '"kms:EncryptionContext:aws:logs:arn" = [\n              for group in values(local.log_groups)',
     '"kms:EncryptionContext:aws:logs:arn" = [\n              for group in ["*"]', PUBLIC),
    ("key_rotation_off", "main.tf", "enable_key_rotation     = true", "enable_key_rotation     = false", PUBLIC),
    ("logs_endpoint_without_private_dns", "endpoints.tf",
     'service_name        = "com.amazonaws.${var.region}.logs"\n  vpc_endpoint_type   = "Interface"\n  subnet_ids          = aws_subnet.app[*].id\n  security_group_ids  = [aws_security_group.endpoints.id]\n  private_dns_enabled = true',
     'service_name        = "com.amazonaws.${var.region}.logs"\n  vpc_endpoint_type   = "Interface"\n  subnet_ids          = aws_subnet.app[*].id\n  security_group_ids  = [aws_security_group.endpoints.id]\n  private_dns_enabled = false', ENDPOINT),
    ("agent_logs_writable_from_outside_the_vpc", "iam.tf",
     '        Condition = { StringEquals = { "aws:SourceVpce" = aws_vpc_endpoint.logs.id } }\n', "", ENDPOINT),
    ("pgaudit_replaces_sql_statistics", "database.tf",
     'value        = "pg_stat_statements,pgaudit"', 'value        = "pgaudit"', PUBLIC),
    ("ddl_and_role_changes_not_audited", "database.tf", 'value        = "ddl,role"', 'value        = "none"', PUBLIC),
    ("query_log_into_the_agent_group", "dns.tf",
     'destination_arn = aws_cloudwatch_log_group.lab["dns"].arn',
     'destination_arn = aws_cloudwatch_log_group.lab["agent"].arn', RESOLVER_LOG),
    ("query_log_group_writable_by_anyone", "dns.tf",
     'Principal = { Service = "delivery.logs.amazonaws.com" }', 'Principal = "*"', RESOLVER_LOG),
    ("empty_model_list_accepted", "variables.tf", "length(var.model_ids) > 0 && ", "", EMPTY_MODELS),
    ("oversized_vpc_accepted", "variables.tf",
     'split("/", var.vpc_cidr)[1] == "16"', 'tonumber(split("/", var.vpc_cidr)[1]) <= 16', LARGE_VPC),
    ("alarm_topic_unencrypted", "alarms.tf", "  kms_master_key_id = aws_kms_key.lab.arn\n", "", ALARMS),
    ("alarm_topic_open_to_other_accounts", "alarms.tf",
     '          StringEquals = { "aws:SourceAccount" = local.account_id }\n', "", ALARMS),
    ("alarm_topic_open_to_any_alarm", "alarms.tf",
     '          ArnEquals    = { "aws:SourceArn" = local.alarm_arns }\n', "", ALARMS),
    ("alarm_topic_publishable_by_identities_in_the_account", "alarms.tf",
     '      {\n        Sid       = "NothingElsePublishes"\n        Effect    = "Deny"\n        Principal = "*"\n'
     '        Action    = "sns:Publish"\n        Resource  = aws_sns_topic.alarms.arn\n'
     '        Condition = { ArnNotEquals = { "aws:SourceArn" = local.alarm_arns } }\n      },\n', "", ALARMS),
    ("alarm_name_the_policies_do_not_list", "alarms.tf",
     'alarm_name          = "${var.name}-db-capacity-${count.index}"', 'alarm_name          = "${var.name}-db-load-${count.index}"', ALARMS),
    ("db_alarm_on_the_cluster_average", "alarms.tf",
     "DBInstanceIdentifier = aws_rds_cluster_instance.lab[count.index].identifier",
     "DBClusterIdentifier = aws_rds_cluster.lab.cluster_identifier", ALARMS),
    ("alarm_actions_disabled", "alarms.tf",
     'treat_missing_data  = "breaching"\n  actions_enabled     = true', 'treat_missing_data  = "breaching"\n  actions_enabled     = false', ALARMS),
    ("alarm_never_reports_clearing", "alarms.tf",
     "  ok_actions          = [aws_sns_topic.alarms.arn]\n\n  dimensions = {\n    FirewallRuleGroupId",
     "\n  dimensions = {\n    FirewallRuleGroupId", ALARMS),
    ("bedrock_alarm_waits_three_periods", "alarms.tf",
     '  metric_name         = each.value\n  statistic           = "Sum"\n  period              = 300\n  evaluation_periods  = 1\n',
     '  metric_name         = each.value\n  statistic           = "Sum"\n  period              = 300\n  evaluation_periods  = 3\n', ALARMS),
    ("dns_alarm_counts_by_the_hour", "alarms.tf",
     '  metric_name         = "FirewallRuleQueryVolume"\n  statistic           = "Sum"\n  period              = 300\n',
     '  metric_name         = "FirewallRuleQueryVolume"\n  statistic           = "Sum"\n  period              = 3600\n', ALARMS),
    ("db_alarm_on_one_minute_spikes", "alarms.tf",
     '  statistic           = "Maximum"\n  period              = 300\n', '  statistic           = "Maximum"\n  period              = 60\n', ALARMS),
    ("bedrock_alarms_filtered_by_model", "alarms.tf",
     "  metric_name         = each.value\n", "  metric_name         = each.value\n  dimensions          = { ModelId = var.model_ids[0] }\n", ALARMS),
    ("dns_alarm_on_the_allowed_list", "alarms.tf",
     "firewall_domain_list.everything.id", "firewall_domain_list.allowed.id", ALARMS),
    ("silent_vector_store_counts_as_fine", "alarms.tf",
     'treat_missing_data  = "breaching"', 'treat_missing_data  = "notBreaching"', ALARMS),
    ("key_lets_any_alarm_use_it", "main.tf",
     '          StringEquals = { "aws:SourceAccount" = local.account_id }\n          ArnEquals    = { "aws:SourceArn" = local.alarm_arns }\n',
     "", PUBLIC),
    ("any_alarm_address_accepted", "variables.tf",
     'can(regex("^[^@\\\\s]+@[^@\\\\s]+\\\\.[^@\\\\s]+$", e))', "true", ALARM_EMAILS),
]

# In the order of tests/design.tftest.hcl.
RUNS = [ENDPOINT, PUBLIC, WAY_OUT, FLOWS, INVOCATION_LOGS, EMPTY_MODELS, ALARMS, ALARM_EMAILS, LARGE_VPC, RESOLVER_LOG]


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def run_outcomes(cwd):
    """Each test run's outcome (pass, fail, skip or error) as terraform test reports it."""
    output = run(["terraform", "test", "-no-color"], cwd).stdout
    return dict(re.findall(r'run "([a-z_]+)"\.\.\. ([a-z]+)', output))


def caught(outcomes, covering):
    """The covering run failed, the runs before it passed, and the runs after it passed or were skipped."""
    if set(outcomes) != set(RUNS) or outcomes[covering] != "fail":
        return False
    position = RUNS.index(covering)
    return (all(outcomes[name] == "pass" for name in RUNS[:position])
            and all(outcomes[name] in ("pass", "skip") for name in RUNS[position + 1:]))


def trivy_high(cwd, report):
    """The number of HIGH or CRITICAL misconfigurations, or None if trivy did not finish."""
    done = run(["trivy", "config", "--skip-check-update", "--skip-version-check", "--quiet",
                "--format", "json", "--output", str(report), "."], cwd)
    if done.returncode != 0 or not report.exists():
        return None
    results = json.loads(report.read_text(encoding="utf-8")).get("Results") or []
    return sum(1 for r in results for m in r.get("Misconfigurations") or []
               if m.get("Status") == "FAIL" and m.get("Severity") in ("HIGH", "CRITICAL"))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        baseline = run_outcomes(SOURCE)
        if baseline != {name: "pass" for name in RUNS}:
            raise SystemExit(f"the unbroken design does not pass every run: {baseline}")
        if trivy_high(SOURCE, Path(tmp) / "baseline.json") != 0:
            raise SystemExit("trivy reports HIGH or CRITICAL findings on the unbroken design, or did not finish")

    results = []
    for case, file, old, new, covering in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "aws"
            shutil.copytree(SOURCE, work, symlinks=True)
            target = work / file
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                raise SystemExit(f"{case}: the text to break occurs {text.count(old)} times in {file}")
            target.write_text(text.replace(old, new), encoding="utf-8")
            valid = run(["terraform", "validate", "-no-color"], work).returncode == 0
            outcomes = run_outcomes(work)
            high = trivy_high(work, Path(tmp) / "trivy.json")
        result = {
            "case": case,
            "file": file,
            "covering_run": covering,
            "still_valid": valid,
            "run_outcomes": outcomes,
            "caught_by_terraform_test": valid and caught(outcomes, covering),
            "trivy_high_or_critical": high,
            "caught_by_trivy_high": bool(high),
        }
        results.append(result)
        print(f"{case:40} valid={valid!s:5} test={result['caught_by_terraform_test']!s:5} "
              f"trivy={'error' if high is None else high}", flush=True)

    caught_count = sum(r["caught_by_terraform_test"] for r in results)
    print(json.dumps({"cases": len(results), "caught_by_terraform_test": caught_count,
                      "caught_by_trivy_high": sum(r["caught_by_trivy_high"] for r in results),
                      "trivy_errors": sum(r["trivy_high_or_critical"] is None for r in results)}))
    if os.environ.get("OUT"):
        Path(os.environ["OUT"]).write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    return 0 if caught_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
