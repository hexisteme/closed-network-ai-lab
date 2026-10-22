"""The AWS design's monthly cost estimate: sums, counts read from Terraform text, missing prices, checks.

The test container mounts infra/aws/cost but not the .tf files, so these tests read only that
directory and write a small stand-in design to a temporary directory. `estimate.py verify`
checks quantities.json against the real .tf files; the checks it runs are tested here on the
stand-in.
"""

import copy
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

COST = Path(__file__).resolve().parents[1] / "infra" / "aws" / "cost"
sys.path.insert(0, str(COST))

import estimate  # noqa: E402

QUANTITIES = json.loads((COST / "quantities.json").read_text(encoding="utf-8"))
PRICES_PATH = estimate.newest_snapshot(COST)
PRICES = json.loads(PRICES_PATH.read_text(encoding="utf-8"))

DESIGN = '''# A stand-in for infra/aws: two zones, two database instances, one topic, four alarms.
variable "availability_zones" {
  default = ["zone-a", "zone-c"]
}

resource "aws_rds_cluster_instance" "lab" {
  count = length(var.availability_zones)
}

resource "aws_sns_topic" "alarms" {
  name = "lab-alarms"
}

resource "aws_cloudwatch_metric_alarm" "bedrock" {
  for_each = { throttles = "InvocationThrottles", errors = "InvocationServerErrors" }
  period   = 300
}

resource "aws_cloudwatch_metric_alarm" "db_capacity" {
  count  = length(aws_rds_cluster_instance.lab)
  period = 300
}
'''
STAND_IN = tempfile.TemporaryDirectory()
TF = Path(STAND_IN.name)
(TF / "design.tf").write_text(DESIGN, encoding="utf-8")


def tearDownModule():
    STAND_IN.cleanup()


def header_lines(text, resource_type):
    pattern = re.compile(rf'^resource "{resource_type}" ', re.MULTILINE)
    return [text.count("\n", 0, m.start()) + 1 for m in pattern.finditer(text)]


def run(quantities=QUANTITIES, prices=PRICES):
    return estimate.estimate(quantities, prices, TF)


class TotalsTest(unittest.TestCase):
    def test_the_fixed_total_is_the_sum_of_its_lines(self):
        fixed = run()["fixed"]
        priced = [Decimal(line["usd"]) for line in fixed["lines"] if line["usd"] is not None]
        self.assertTrue(priced)
        self.assertEqual(sum(priced, Decimal(0)), Decimal(fixed["total_usd"]))

    def test_each_scenario_total_is_the_sum_of_its_lines(self):
        result = run()
        self.assertEqual(set(result["scenarios"]), set(QUANTITIES["scenarios"]))
        for name, scenario in result["scenarios"].items():
            priced = [Decimal(line["usd"]) for line in scenario["lines"] if line["usd"] is not None]
            self.assertEqual(sum(priced, Decimal(0)), Decimal(scenario["total_usd"]), name)
            self.assertEqual(Decimal(scenario["with_fixed_usd"]),
                             Decimal(scenario["total_usd"]) + Decimal(result["fixed"]["total_usd"]), name)

    def test_a_line_is_its_quantity_times_the_published_rate(self):
        for line in run()["fixed"]["lines"]:
            if line["status"] != "priced":
                continue
            price = PRICES["prices"][line["price"]]
            self.assertEqual(len(price["dimensions"]), 1, line["id"])
            self.assertEqual(line["sku"], price["sku"], line["id"])
            rate = Decimal(price["dimensions"][0]["usd"])
            expected = (Decimal(str(line["quantity"])) * rate).quantize(Decimal("0.0001"))
            self.assertEqual(Decimal(line["usd"]), expected, line["id"])

    def test_tiers_are_applied_in_order(self):
        price = {"unit": "Requests", "dimensions": [
            {"beginRange": "1000", "endRange": "Inf", "usd": "0.5"},
            {"beginRange": "0", "endRange": "1000", "usd": "0"},
        ]}
        self.assertEqual(estimate.tiered_cost(price, Decimal(999)), Decimal(0))
        self.assertEqual(estimate.tiered_cost(price, Decimal(1010)), Decimal(5))

    def test_a_quantity_past_the_last_tier_is_refused(self):
        price = {"unit": "Alarms", "dimensions": [{"beginRange": "0", "endRange": "10", "usd": "0"}]}
        with self.assertRaises(estimate.EstimateError):
            estimate.tiered_cost(price, Decimal(11))

    def test_every_usage_quantity_has_a_formula_and_every_scenario_all_inputs(self):
        self.assertEqual(len([i for i in QUANTITIES["usage"] if i["id"].startswith("log_ingest_")]), 5)
        for item in QUANTITIES["usage"]:
            self.assertIn(item["quantity_of"], estimate.FORMULAS, item["id"])
        for name, inputs in QUANTITIES["scenarios"].items():
            self.assertEqual(set(inputs) - {"about"}, set(QUANTITIES["scenario_inputs"]), name)
        for name, assumption in QUANTITIES["assumptions"].items():
            self.assertTrue(assumption["basis"], name)


class CountedFromTerraformTest(unittest.TestCase):
    def line(self, item_id):
        return next(line for line in run()["fixed"]["lines"] if line["id"] == item_id)

    def test_alarms_are_counted_from_the_resource_blocks(self):
        line = self.line("cloudwatch_alarms")
        blocks = [f"design.tf:{n}" for n in header_lines(DESIGN, "aws_cloudwatch_metric_alarm")]
        self.assertEqual([(b["at"], b["instances"]) for b in line["counted_blocks"]], list(zip(blocks, [2, 2])))
        self.assertEqual(line["quantity"], 4)
        self.assertEqual(line["cites"][:2], blocks)

    def test_sns_topics_are_counted_from_the_resource_blocks(self):
        line = self.line("sns_topics")
        self.assertEqual([b["at"] for b in line["counted_blocks"]],
                         [f"design.tf:{n}" for n in header_lines(DESIGN, "aws_sns_topic")])
        self.assertEqual(line["quantity"], 1)
        self.assertEqual(line["status"], "no charge")

    def test_a_for_each_map_counts_its_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.tf").write_text(
                'variable "emails" {\n  default = ["a@x.io", "b@x.io"]\n}\n'
                'resource "aws_cloudwatch_metric_alarm" "many" {\n'
                '  for_each = {\n    one = "A"  # a comment with { braces\n    two = "B"\n    three = "C"\n  }\n'
                '  alarm_name = "${var.name}-${replace(each.key, "_", "-")}"\n  period     = 300\n}\n'
                'resource "aws_cloudwatch_metric_alarm" "twice" {\n  count  = 2\n  period = 60\n}\n'
                'resource "aws_cloudwatch_metric_alarm" "single" {\n  period = 300\n'
                '  dimensions = {\n    count = "not a meta-argument"\n  }\n}\n'
                'resource "aws_sns_topic_subscription" "mail" {\n  for_each = toset(var.emails)\n}\n'
                'resource "aws_cloudwatch_metric_alarm" "per_mail" {\n'
                '  count  = length(aws_sns_topic_subscription.mail)\n  period = 300\n}\n',
                encoding="utf-8",
            )
            alarms = estimate.count_resource(Path(tmp), "aws_cloudwatch_metric_alarm")
            mail = estimate.count_resource(Path(tmp), "aws_sns_topic_subscription")
        self.assertEqual([(b["at"], b["instances"]) for b in alarms],
                         [("a.tf:4", 3), ("a.tf:13", 2), ("a.tf:17", 1), ("a.tf:26", 2)])
        self.assertEqual([b["instances"] for b in mail], [2])

    def test_comments_strings_and_heredocs_are_not_read_as_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.tf").write_text(
                '# resource "aws_cloudwatch_metric_alarm" "commented" {\n'
                '/* resource "aws_cloudwatch_metric_alarm" "block_comment" {\n */\n'
                'resource "aws_iam_role_policy" "p" {\n'
                '  policy = <<EOT\nresource "aws_cloudwatch_metric_alarm" "in_heredoc" {\nEOT\n'
                '  name = "resource \\"aws_cloudwatch_metric_alarm\\" \\"x\\" {"\n'
                '  # a stray } in a comment\n}\n'
                'resource "aws_cloudwatch_metric_alarm" "real" {\n  period = 300\n}\n',
                encoding="utf-8",
            )
            alarms = estimate.count_resource(Path(tmp), "aws_cloudwatch_metric_alarm")
        self.assertEqual([(b["name"], b["at"]) for b in alarms], [("real", "a.tf:11")])

    def test_an_alarm_it_cannot_price_is_refused(self):
        bodies = {
            "metric math": '  metric_query {\n    id = "m1"\n  }\n',
            "anomaly detection": '  threshold_metric_id = "ad1"\n  period = 300\n',
            "high resolution": "  period = 10\n",
            "uncountable": "  for_each = data.aws_x.y.ids\n  period = 300\n",
            "variable without a default": "  count = length(var.zones)\n  period = 300\n",
        }
        for label, body in bodies.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "a.tf").write_text(
                    f'variable "zones" {{\n  type = list(string)\n}}\n'
                    f'resource "aws_cloudwatch_metric_alarm" "x" {{\n{body}}}\n', encoding="utf-8")
                with self.assertRaises(estimate.EstimateError):
                    estimate.count_resource(Path(tmp), "aws_cloudwatch_metric_alarm")


class VerifyTest(unittest.TestCase):
    def test_every_cite_names_a_line_and_quotes_it(self):
        cites = [c for s in ("factors", "fixed", "usage") for item in QUANTITIES[s] for c in item.get("cites", [])]
        self.assertTrue(cites)
        for cite in cites:
            self.assertRegex(cite["at"], r"^[a-z_]+\.tf:[1-9]\d*$")
            self.assertTrue(cite["text"].strip(), cite["at"])

    def test_a_cite_that_no_longer_holds_its_text_is_reported(self):
        quantities = {"factors": [], "usage": [], "fixed": [{"id": "alarm", "cites": [
            {"at": "design.tf:20", "text": "count  = length(aws_rds_cluster_instance.lab)"}]}]}
        self.assertEqual(estimate.check_cites(TF, quantities), [])
        quantities["fixed"][0]["cites"] = [
            {"at": "design.tf:21", "text": "count  = length(aws_rds_cluster_instance.lab)"},
            {"at": "design.tf:999", "text": "period"},
            {"at": "gone.tf:1", "text": "period"},
        ]
        self.assertEqual(len(estimate.check_cites(TF, quantities)), 3)

    def test_a_resource_type_without_a_line_or_a_reason_is_reported(self):
        terraform = estimate.Terraform(TF)
        self.assertEqual(estimate.check_accounting(terraform, QUANTITIES), [])
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.tf").write_text(DESIGN + 'resource "aws_nat_gateway" "out" {\n}\n', encoding="utf-8")
            problems = estimate.check_accounting(estimate.Terraform(tmp), QUANTITIES)
        self.assertEqual(len(problems), 1)
        self.assertIn("aws_nat_gateway", problems[0])


class MissingPriceTest(unittest.TestCase):
    def test_a_missing_price_is_reported_not_zeroed(self):
        prices = copy.deepcopy(PRICES)
        del prices["prices"]["aurora_acu_hour"]
        result = run(prices=prices)
        line = next(line for line in result["fixed"]["lines"] if line["id"] == "aurora_minimum_acu_hours")
        self.assertEqual(line["status"], "not priced")
        self.assertIsNone(line["usd"])
        self.assertIn("aurora_acu_hour", line["reason"])
        self.assertIn("aurora_minimum_acu_hours", result["fixed"]["not_priced"])
        self.assertLess(Decimal(result["fixed"]["total_usd"]), Decimal(run()["fixed"]["total_usd"]))

    def test_a_unit_that_differs_from_the_sku_is_refused(self):
        quantities = copy.deepcopy(QUANTITIES)
        next(i for i in quantities["fixed"] if i["id"] == "kms_key")["unit"] = "Hrs"
        with self.assertRaises(estimate.EstimateError):
            run(quantities=quantities)

    def test_bedrock_is_a_formula_without_a_number(self):
        for name, scenario in run()["scenarios"].items():
            bedrock = [line for line in scenario["lines"] if line["id"].startswith("bedrock_")]
            self.assertTrue(bedrock, name)
            for line in bedrock:
                self.assertEqual(line["status"], "not priced")
                self.assertIsNone(line["usd"])
                self.assertTrue(line["reason"])
                self.assertIn("formula", line)
                self.assertIn(line["id"], scenario["not_priced"])


class SnapshotTest(unittest.TestCase):
    def test_every_price_names_its_offer_sku_and_source(self):
        self.assertEqual(PRICES["source"], "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json")
        self.assertEqual(PRICES["region"], "ap-northeast-2")
        for key, price in PRICES["prices"].items():
            offer = PRICES["offers"][price["offer"]]
            self.assertRegex(offer["sha256"], r"^[0-9a-f]{64}$", key)
            self.assertTrue(offer["publicationDate"] and offer["version"], key)
            self.assertTrue(offer["offer_url"].startswith("https://pricing.us-east-1.amazonaws.com/"), key)
            self.assertRegex(price["sku"], r"^[A-Z0-9]{16}$", key)
            for dimension in price["dimensions"]:
                Decimal(dimension["usd"])
                self.assertEqual(dimension["unit"], price["unit"], key)

    def test_the_snapshot_holds_only_the_prices_the_quantities_name(self):
        named = {item["price"] for s in ("fixed", "usage") for item in QUANTITIES[s] if item.get("price")}
        named.update(QUANTITIES["free_tiers"])
        self.assertEqual(set(PRICES["prices"]), set(QUANTITIES["prices"]))
        self.assertLessEqual(named, set(QUANTITIES["prices"]))


class CommandTest(unittest.TestCase):
    def test_the_command_prints_a_table_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(estimate, "verify", return_value=[]):
            code = estimate.main(["--evidence-dir", tmp, "--tf-dir", str(TF)])
            written = list(Path(tmp).glob("cost-estimate-*.json"))
            data = json.loads(written[0].read_text(encoding="utf-8")) if written else None
        self.assertEqual(code, 0)
        self.assertEqual(len(written), 1)
        self.assertRegex(written[0].name, r"^cost-estimate-\d{8}T\d{6}Z\.json$")
        self.assertEqual(data["hours_per_month"], 730)
        self.assertEqual(data["prices_file"], PRICES_PATH.name)
        self.assertEqual(set(data["terraform_sha256"]), {"design.tf"})
        self.assertIn("Fixed monthly total", out.getvalue())
        self.assertIn(data["fixed"]["total_usd"], out.getvalue())

    def test_the_command_refuses_when_the_quantities_do_not_match_the_terraform(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", new=io.StringIO()) as err, \
                mock.patch.object(estimate, "verify", return_value=["kms_key: main.tf:33 does not exist"]):
            code = estimate.main(["--evidence-dir", tmp, "--tf-dir", str(TF)])
            written = list(Path(tmp).iterdir())
        self.assertEqual(code, 1)
        self.assertEqual(written, [])
        self.assertIn("main.tf:33", err.getvalue())


if __name__ == "__main__":
    unittest.main()
