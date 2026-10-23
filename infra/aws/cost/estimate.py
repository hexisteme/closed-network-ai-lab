"""Monthly cost of the AWS design in infra/aws, from its Terraform and a snapshot of public prices.

The design is not deployed, and nothing here uses an AWS account or the network. quantities.json
says what the Terraform is billed for, with the line that creates each item; the newest
prices-<region>-<YYYYMMDD>.json beside it (written by snapshot.py from the public Price List)
holds the rates. Fixed lines bill for a 730-hour month whether or not anything runs; scenario
lines price usage under the assumptions quantities.json states. Alarms and SNS topics are counted
from the .tf files. A quantity with no price is reported as not priced, never as zero.

  python3 -I infra/aws/cost/estimate.py verify    check quantities.json against the .tf files
  python3 -I infra/aws/cost/estimate.py           verify, print the table, write
                                                  evidence/cost-estimate-<UTC stamp>.json
Standard library only.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[2]
CENT = Decimal("0.0001")
SNAPSHOT = re.compile(r"prices-[a-z0-9-]+-\d{8}\.json")
HEADER = re.compile(r'^[ \t]*([A-Za-z_][\w-]*)((?:[ \t]+"[^"\n]*")*)[ \t]*\{', re.MULTILINE)
ATTRIBUTE = re.compile(r"[ \t]*([A-Za-z_][\w-]*)[ \t]*=(?![=>])")
NESTED = re.compile(r'[ \t]*([A-Za-z_][\w-]*)(?:[ \t]+"[^"\n]*")*[ \t]*\{')
HEREDOC = re.compile(r"<<-?([A-Za-z_][\w-]*)[ \t]*\n")
MAP_KEY = re.compile(r'\s*(?:[A-Za-z_][\w-]*|"[^"\n]*")\s*[=:]')
FORMULAS = {
    "model_calls": "agent_runs x model_calls_per_run",
    "embedding_calls": "agent_runs x embedding_calls_per_run",
    "log_gb.flow": "agent_runs x flow_log_bytes_per_run / bytes_per_gb",
    "log_gb.agent": "agent_runs x agent_log_bytes_per_run / bytes_per_gb",
    "log_gb.invocations": "(model_calls + embedding_calls) x invocation_log_bytes_per_call / bytes_per_gb",
    "log_gb.dns": "agent_runs x dns_queries_per_run x dns_log_bytes_per_query / bytes_per_gb",
    "log_gb.postgresql": "agent_runs x postgresql_log_bytes_per_run / bytes_per_gb",
    "log_storage_gb_month": "sum of log_gb x log_retention_days x 24 / hours_per_month x log_stored_fraction",
    "endpoint_gb": "((model_calls + embedding_calls) x endpoint_bytes_per_call + agent_runs x agent_log_bytes_per_run) / bytes_per_gb",
    "dns_queries": "agent_runs x dns_queries_per_run",
    "aurora_storage_gb": "aurora_storage_gb",
    "aurora_io_requests": "aurora_io_requests",
    "aurora_backup_billed_gb": "aurora_backup_billed_gb",
    "aurora_acu_hours_above_minimum": "aurora_instances x (aurora_average_acu_per_instance - aurora_minimum_acu) x hours_per_month",
    "kms_requests": "kms_requests + alarm_notifications x kms_requests_per_publish",
    "secrets_api_requests": "secrets_api_requests",
    "sns_publishes": "alarm_notifications",
    "sns_email_deliveries": "alarm_notifications x alarm_email_addresses",
    "input_tokens": "model_calls x input_tokens_per_call",
    "output_tokens": "model_calls x output_tokens_per_call",
    "embedding_tokens": "embedding_calls x embedding_tokens_per_call",
}


class EstimateError(Exception):
    """A quantity, count or price the estimate cannot stand behind."""


def mask(text: str) -> str:
    """The text with comments, string contents and heredoc bodies blanked, offsets and newlines kept."""
    out = list(text)

    def blank(start, end):
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    i = 0
    while i < len(text):
        if text[i] == "#" or text.startswith("//", i):
            end = text.find("\n", i)
            end = len(text) if end == -1 else end
            blank(i, end)
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = len(text) if end == -1 else end + 2
            blank(i, end)
            i = end
        elif heredoc := HEREDOC.match(text, i):
            closing = re.compile(rf"^[ \t]*{re.escape(heredoc.group(1))}[ \t]*$", re.MULTILINE).search(text, heredoc.end())
            if closing is None:
                raise EstimateError(f"heredoc {heredoc.group(1)} is never closed")
            blank(heredoc.end(), closing.start())
            i = closing.end()
        elif text[i] == '"':
            end = string_end(text, i)
            blank(i + 1, end - 1)
            i = end
        else:
            i += 1
    return "".join(out)


def string_end(text: str, i: int) -> int:
    """Offset just past the quoted string opening at i, stepping over ${...} and %{...}."""
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
        elif text[j] == '"':
            return j + 1
        elif text.startswith(("$${", "%%{"), j):
            j += 3
        elif text.startswith(("${", "%{"), j):
            j = template_end(text, j + 2)
        else:
            j += 1
    raise EstimateError("a quoted string is never closed")


def template_end(text: str, j: int) -> int:
    depth = 1
    while j < len(text):
        if text[j] == '"':
            j = string_end(text, j)
            continue
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise EstimateError("a template interpolation is never closed")


def closing_brace(masked: str, opening: int) -> int:
    depth = 0
    for k in range(opening, len(masked)):
        if masked[k] == "{":
            depth += 1
        elif masked[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    raise EstimateError("a block is never closed")


def read_blocks(tf_dir) -> list:
    """Top-level blocks of every .tf file: keyword, type and name, file:line, body and masked body."""
    blocks = []
    for path in sorted(Path(tf_dir).glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        masked = mask(text)
        position = 0
        for header in HEADER.finditer(text):
            if header.start() < position or masked[header.start(1)] != text[header.start(1)]:
                continue
            opening = header.end() - 1
            end = closing_brace(masked, opening)
            labels = re.findall(r'"([^"\n]*)"', header.group(2))
            keyword = header.group(1)
            typed = keyword in ("resource", "data")
            blocks.append({
                "keyword": keyword,
                "type": labels[0] if typed and labels else None,
                "name": labels[-1] if labels else None,
                "at": f"{path.name}:{text.count(chr(10), 0, header.start()) + 1}",
                "body": text[opening + 1:end],
                "masked": masked[opening + 1:end],
            })
            position = end + 1
    return blocks


def expression_end(masked: str, start: int) -> int:
    depth = 0
    for k in range(start, len(masked)):
        if masked[k] in "{[(":
            depth += 1
        elif masked[k] in "}])":
            depth -= 1
        elif masked[k] == "\n" and depth <= 0:
            return k
    return len(masked)


def trimmed(text: str, masked: str, start: int, end: int) -> tuple:
    """The (text, masked) pair between start and end, trimmed by the masked side so offsets agree."""
    piece = masked[start:end]
    lead = len(piece) - len(piece.lstrip())
    tail = len(piece.rstrip())
    return text[start + lead:start + tail], masked[start + lead:start + tail]


def top_level(block: dict) -> tuple:
    """A block body's own attributes, {name: (text, masked)}, and the names of its nested blocks."""
    text, masked = block["body"], block["masked"]
    attributes, nested = {}, []
    depth, i, line_start = 0, 0, True
    while i < len(masked):
        if line_start and depth == 0:
            attribute = ATTRIBUTE.match(masked, i)
            if attribute:
                end = expression_end(masked, attribute.end())
                attributes[attribute.group(1)] = trimmed(text, masked, attribute.end(), end)
                i, line_start = end, False
                continue
            block_header = NESTED.match(masked, i)
            if block_header:
                nested.append(block_header.group(1))
        if masked[i] in "{[(":
            depth += 1
        elif masked[i] in "}])":
            depth -= 1
        line_start = masked[i] == "\n"
        i += 1
    return attributes, nested


def encloses(masked: str) -> bool:
    """Whether the first bracket closes only at the last character, as in [a, b] and not [a] + [b]."""
    depth = 0
    for k, char in enumerate(masked):
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
            if depth == 0 and k != len(masked) - 1:
                return False
    return depth == 0


def literal_elements(text: str, masked: str) -> list:
    """The elements of a [...] list or the keys of a {...} map, as their source text."""
    if masked[0] + masked[-1] not in ("[]", "{}") or not encloses(masked):
        raise EstimateError(f"not a literal list or map: {text}")
    inner = masked[1:-1]
    if re.match(r"\s*for\s", inner):
        raise EstimateError(f"a for expression is not counted: {text}")
    pieces, depth, start = [], 0, 1
    separators = ",\n" if masked[0] == "{" else ","
    for k, char in enumerate(inner, start=1):
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char in separators and depth == 0:
            pieces.append(trimmed(text, masked, start, k))
            start = k + 1
    pieces.append(trimmed(text, masked, start, len(masked) - 1))
    pieces = [(t, m) for t, m in pieces if m]
    if masked[0] == "{":
        if not all(MAP_KEY.match(m) for _, m in pieces):
            raise EstimateError(f"not a map literal: {text}")
        return [re.split(r"\s*[=:]", t, maxsplit=1)[0].strip() for t, _ in pieces]
    return [t for t, _ in pieces]


class Terraform:
    """The .tf files of one directory, read as text, with what each count and for_each comes to."""

    def __init__(self, tf_dir):
        self.dir = Path(tf_dir)
        self.blocks = read_blocks(self.dir)

    def find(self, keyword: str, type_: str, name: str) -> dict:
        for block in self.blocks:
            if block["keyword"] == keyword and block["type"] == type_ and block["name"] == name:
                return block
        raise EstimateError(f"no {keyword} {' '.join(filter(None, (type_, name)))} in {self.dir}")

    def value(self, reference: str) -> tuple:
        """The (text, masked) expression a var.X default or a local.X stands for."""
        kind, _, name = reference.partition(".")
        if kind == "var":
            attributes, _ = top_level(self.find("variable", None, name))
            if "default" not in attributes:
                raise EstimateError(f"var.{name} has no default, so it cannot be counted")
            return attributes["default"]
        for block in self.blocks:
            if block["keyword"] == "locals":
                attributes, _ = top_level(block)
                if name in attributes:
                    return attributes[name]
        raise EstimateError(f"no local.{name}")

    def elements(self, text: str, masked: str, seen=()) -> list:
        """What a collection expression holds: a literal, toset(...), var.X, local.X or a resource's instances."""
        text, masked = trimmed(text, masked, 0, len(masked))
        expression = masked
        if not expression:
            raise EstimateError("an empty expression cannot be counted")
        if expression[0] in "[{":
            return literal_elements(text, masked)
        call = re.fullmatch(r"(toset|tolist)\((.*)\)", expression, re.S)
        if call:
            offset = len(call.group(1)) + 1
            inner = self.elements(text[offset:-1], masked[offset:-1], seen)
            return sorted(set(inner)) if call.group(1) == "toset" else inner
        reference = re.fullmatch(r"(var|local)\.[A-Za-z_][\w-]*", expression)
        if reference:
            if expression in seen:
                raise EstimateError(f"{expression} refers to itself")
            return self.elements(*self.value(expression), seen=(*seen, expression))
        resource = re.fullmatch(r"(aws_[\w]+)\.([A-Za-z_][\w-]*)", expression)
        if resource:
            return list(range(self.instances(resource.group(1), resource.group(2), seen)))
        raise EstimateError(f"cannot count {text.strip()}")

    def count_of(self, block: dict, seen=()) -> int:
        attributes, _ = top_level(block)
        if "count" in attributes:
            text, masked = attributes["count"]
            if re.fullmatch(r"\d+", masked):
                return int(masked)
            length = re.fullmatch(r"length\((.*)\)", masked, re.S)
            if length:
                return len(self.elements(text[7:-1], masked[7:-1], seen))
            raise EstimateError(f"{block['at']}: cannot count count = {text}")
        if "for_each" in attributes:
            try:
                return len(self.elements(*attributes["for_each"], seen=seen))
            except EstimateError as exc:
                raise EstimateError(f"{block['at']}: {exc}") from exc
        return 1

    def instances(self, type_: str, name: str, seen=()) -> int:
        key = f"{type_}.{name}"
        if key in seen:
            raise EstimateError(f"{key} counts itself")
        return self.count_of(self.find("resource", type_, name), (*seen, key))

    def resource_types(self) -> set:
        return {b["type"] for b in self.blocks if b["keyword"] == "resource"}


def check_standard_alarm(block: dict) -> None:
    """Refuse an alarm the standard-resolution, one-metric price does not cover."""
    attributes, nested = top_level(block)
    if "metric_query" in nested:
        raise EstimateError(f"{block['at']}: metric math alarms bill per metric; count them by hand")
    if "threshold_metric_id" in attributes:
        raise EstimateError(f"{block['at']}: an anomaly detection alarm bills as three metrics")
    period = attributes.get("period", ("", ""))[1]
    if not re.fullmatch(r"\d+", period):
        raise EstimateError(f"{block['at']}: an alarm without a literal period cannot be priced")
    if int(period) < 60:
        raise EstimateError(f"{block['at']}: a period under 60 s is a high-resolution alarm, priced higher")


CHECKS = {"aws_cloudwatch_metric_alarm": check_standard_alarm}


def count_resource(tf_dir, type_: str, terraform=None) -> list:
    """Every resource block of one type, with its file:line, name and number of instances."""
    terraform = terraform or Terraform(tf_dir)
    counted = []
    for block in terraform.blocks:
        if block["keyword"] == "resource" and block["type"] == type_:
            if type_ in CHECKS:
                CHECKS[type_](block)
            counted.append({"at": block["at"], "name": block["name"], "instances": terraform.count_of(block)})
    return counted


def instances(tf_dir, type_: str, name: str) -> int:
    return Terraform(tf_dir).instances(type_, name)


def check_cites(tf_dir, quantities: dict) -> list:
    """Problems with the citations: a file or line that is missing, or a line without the quoted text."""
    problems = []
    for section in ("factors", "fixed", "usage"):
        for item in quantities[section]:
            for cite in item.get("cites", []):
                name, _, number = cite["at"].partition(":")
                path = Path(tf_dir) / name
                lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
                if not (number.isdigit() and 1 <= int(number) <= len(lines)):
                    problems.append(f"{item['id']}: {cite['at']} does not exist")
                elif cite["text"] not in lines[int(number) - 1]:
                    problems.append(f"{item['id']}: {cite['at']} no longer reads {cite['text']!r}")
    return problems


def check_accounting(terraform: Terraform, quantities: dict) -> list:
    """Resource types in the Terraform that no line prices and no reason excuses."""
    named = set(quantities["no_charge"])
    for section in ("fixed", "usage"):
        for item in quantities[section]:
            named.update(item.get("resources", []))
    return [f"{t}: no line prices it and no_charge gives no reason" for t in sorted(terraform.resource_types() - named)]


def check_design(terraform: Terraform, quantities: dict) -> list:
    """Static quantities and factors recomputed from the Terraform."""
    problems = []
    fixed = {item["id"]: item for item in quantities["fixed"]}
    factors = {f["id"]: Decimal(str(f["value"])) for f in quantities["factors"]}
    hours = quantities["hours_per_month"]

    def expect(label, stated, actual):
        if Decimal(str(stated)) != Decimal(str(actual)):
            problems.append(f"{label}: quantities.json says {stated}, the Terraform gives {actual}")

    cluster = next(b for b in terraform.blocks if b["keyword"] == "resource" and b["type"] == "aws_rds_cluster")
    cluster_attributes, _ = top_level(cluster)
    minimum = Decimal(re.search(r"min_capacity\s*=\s*([0-9.]+)", cluster["masked"]).group(1))
    db = terraform.instances("aws_rds_cluster_instance", "lab")
    zones = len(terraform.elements(*terraform.value("var.availability_zones")))
    retention = terraform.value("var.log_retention_days")[1]
    interface = [
        b for b in terraform.blocks
        if b["type"] == "aws_vpc_endpoint" and re.search(r'vpc_endpoint_type\s*=\s*"Interface"', b["body"])
    ]
    expect("availability_zones", factors["availability_zones"], zones)
    expect("aurora_instances", factors["aurora_instances"], db)
    expect("aurora_minimum_acu", factors["aurora_minimum_acu"], minimum)
    expect("log_retention_days", factors["log_retention_days"], retention)
    expect("interface_endpoint_hours", fixed["interface_endpoint_hours"]["quantity"],
           len(interface) * terraform.instances("aws_subnet", "app") * hours)
    expect("aurora_minimum_acu_hours", fixed["aurora_minimum_acu_hours"]["quantity"], db * minimum * hours)
    expect("kms_key", fixed["kms_key"]["quantity"],
           sum(b["instances"] for b in count_resource(None, "aws_kms_key", terraform)))
    domains = sum(
        len(terraform.elements(*top_level(b)[0]["domains"]))
        for b in terraform.blocks if b["type"] == "aws_route53_resolver_firewall_domain_list"
    )
    expect("dns_firewall_domain_names", fixed["dns_firewall_domain_names"]["quantity"], domains)
    groups = terraform.instances("aws_cloudwatch_log_group", "lab")
    ingest = [item for item in quantities["usage"] if item["id"].startswith("log_ingest_")]
    expect("log_ingest lines", len(ingest), groups)
    if cluster_attributes.get("manage_master_user_password", ("", ""))[1] != "true":
        problems.append("db_master_secret: the cluster no longer keeps its master password in Secrets Manager")
    if "storage_type" in cluster_attributes:
        problems.append("aurora_storage: storage_type is set; the estimate assumes Aurora Standard")
    return problems


def verify(tf_dir, quantities: dict) -> list:
    """Every problem between quantities.json and the .tf files; empty when they agree."""
    terraform = Terraform(tf_dir)
    problems = check_cites(tf_dir, quantities) + check_accounting(terraform, quantities)
    try:
        problems += check_design(terraform, quantities)
        for item in quantities["fixed"]:
            if "count_from_tf" in item:
                count_resource(None, item["count_from_tf"], terraform)
    except (EstimateError, KeyError, StopIteration, AttributeError) as exc:
        problems.append(f"cannot recompute from the Terraform: {exc!r}")
    return problems


def tiered_cost(price: dict, quantity: Decimal) -> Decimal:
    """Quantity priced through the published tiers in order, each tier from its beginRange."""
    cost, end = Decimal(0), Decimal(0)
    for dimension in sorted(price["dimensions"], key=lambda d: Decimal(d["beginRange"])):
        begin = Decimal(dimension["beginRange"])
        end = None if dimension["endRange"] == "Inf" else Decimal(dimension["endRange"])
        top = quantity if end is None else min(quantity, end)
        if top > begin:
            cost += (top - begin) * Decimal(dimension["usd"])
    if end is not None and quantity > end:
        raise EstimateError(f"{quantity} {price['unit']} runs past the last published tier")
    return cost


def money(amount: Decimal) -> str:
    return str(amount.quantize(CENT, rounding=ROUND_HALF_UP))


def number(amount: Decimal):
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def rate_text(price: dict) -> str:
    dimensions = price["dimensions"]
    if len(dimensions) == 1:
        return f"{Decimal(dimensions[0]['usd']).normalize():f}/{price['unit']}"
    tiers = ", ".join(
        f"{Decimal(d['usd']).normalize():f} from {Decimal(d['beginRange']).normalize():f}" for d in dimensions
    )
    return f"tiered {tiers} {price['unit']}"


def price_line(item: dict, quantity: Decimal, prices: dict, cites: list) -> dict:
    """One line: the quantity at its published rate, or why it carries no charge or no price."""
    line = {"id": item["id"], "what": item["what"], "quantity": number(quantity), "unit": item["unit"],
            "price": item.get("price"), "cites": cites}
    key = item.get("price")
    if key is None:
        if item.get("not_priced_reason"):
            line.update(status="not priced", usd=None, reason=item["not_priced_reason"])
        elif item.get("no_charge_reason"):
            line.update(status="no charge", usd=money(Decimal(0)), reason=item["no_charge_reason"])
        else:
            raise EstimateError(f"{item['id']}: no price and no reason")
        if "formula" in item:
            line["formula"] = item["formula"]
        return line
    price = prices["prices"].get(key)
    if price is None:
        line.update(status="not priced", usd=None, reason=f"no price {key} in the snapshot")
        return line
    if price["unit"] != item["unit"]:
        raise EstimateError(f"{item['id']}: counted in {item['unit']}, but SKU {price['sku']} is priced per {price['unit']}")
    line.update(status="priced", usd=money(tiered_cost(price, quantity)), sku=price["sku"],
                usagetype=price["attributes"].get("usagetype"), rate=rate_text(price))
    return line


def summarize(lines: list) -> dict:
    total = sum((Decimal(line["usd"]) for line in lines if line["usd"] is not None), Decimal(0))
    return {"lines": lines, "total_usd": money(total),
            "not_priced": [line["id"] for line in lines if line["status"] == "not priced"]}


def scenario_amounts(quantities: dict, inputs: dict) -> dict:
    """The usage quantities of one scenario, by the names FORMULAS gives."""
    a = {name: Decimal(str(entry["value"])) for name, entry in quantities["assumptions"].items()}
    f = {factor["id"]: Decimal(str(factor["value"])) for factor in quantities["factors"]}
    s = {name: Decimal(str(value)) for name, value in inputs.items() if name != "about"}
    gb = Decimal(quantities["bytes_per_gb"])
    hours = Decimal(quantities["hours_per_month"])
    if s["aurora_average_acu_per_instance"] < f["aurora_minimum_acu"]:
        raise EstimateError("an instance never runs below its minimum capacity")
    runs = s["agent_runs"]
    calls = runs * a["model_calls_per_run"]
    embeds = runs * a["embedding_calls_per_run"]
    amounts = {
        "model_calls": calls,
        "embedding_calls": embeds,
        "log_gb.flow": runs * a["flow_log_bytes_per_run"] / gb,
        "log_gb.agent": runs * a["agent_log_bytes_per_run"] / gb,
        "log_gb.invocations": (calls + embeds) * a["invocation_log_bytes_per_call"] / gb,
        "log_gb.dns": runs * a["dns_queries_per_run"] * a["dns_log_bytes_per_query"] / gb,
        "log_gb.postgresql": runs * a["postgresql_log_bytes_per_run"] / gb,
        "endpoint_gb": ((calls + embeds) * a["endpoint_bytes_per_call"] + runs * a["agent_log_bytes_per_run"]) / gb,
        "dns_queries": runs * a["dns_queries_per_run"],
        "aurora_storage_gb": s["aurora_storage_gb"],
        "aurora_io_requests": s["aurora_io_requests"],
        "aurora_backup_billed_gb": s["aurora_backup_billed_gb"],
        "aurora_acu_hours_above_minimum":
            f["aurora_instances"] * (s["aurora_average_acu_per_instance"] - f["aurora_minimum_acu"]) * hours,
        "kms_requests": s["kms_requests"] + s["alarm_notifications"] * a["kms_requests_per_publish"],
        "secrets_api_requests": s["secrets_api_requests"],
        "sns_publishes": s["alarm_notifications"],
        "sns_email_deliveries": s["alarm_notifications"] * s["alarm_email_addresses"],
        "input_tokens": calls * a["input_tokens_per_call"],
        "output_tokens": calls * a["output_tokens_per_call"],
        "embedding_tokens": embeds * a["embedding_tokens_per_call"],
    }
    logs = sum(value for name, value in amounts.items() if name.startswith("log_gb."))
    amounts["log_storage_gb_month"] = logs * f["log_retention_days"] * 24 / hours * a["log_stored_fraction"]
    return amounts


def estimate(quantities: dict, prices: dict, tf_dir) -> dict:
    """Fixed lines and per-scenario usage lines, each with its total and the ids left unpriced."""
    if prices["region"] != quantities["region"]:
        raise EstimateError(f"prices are for {prices['region']}, quantities for {quantities['region']}")
    terraform = Terraform(tf_dir)
    fixed_lines = []
    for item in quantities["fixed"]:
        cites = [cite["at"] for cite in item.get("cites", [])]
        if "count_from_tf" in item:
            counted = count_resource(None, item["count_from_tf"], terraform)
            quantity = Decimal(sum(block["instances"] for block in counted))
            line = price_line(item, quantity, prices, [block["at"] for block in counted] + cites)
            line["counted_blocks"] = counted
        else:
            line = price_line(item, Decimal(str(item["quantity"])), prices, cites)
        fixed_lines.append(line)
    fixed = summarize(fixed_lines)
    scenarios = {}
    for name, inputs in quantities["scenarios"].items():
        amounts = scenario_amounts(quantities, inputs)
        lines = []
        for item in quantities["usage"]:
            if item["quantity_of"] not in amounts:
                raise EstimateError(f"{item['id']}: no formula for {item['quantity_of']}")
            lines.append(price_line(item, amounts[item["quantity_of"]], prices, [c["at"] for c in item["cites"]]))
        scenario = summarize(lines)
        scenario["about"] = inputs["about"]
        scenario["with_fixed_usd"] = money(Decimal(scenario["total_usd"]) + Decimal(fixed["total_usd"]))
        scenario["amounts"] = {key: number(value) for key, value in amounts.items()}
        scenarios[name] = scenario
    free = []
    for key in quantities["free_tiers"]:
        price = prices["prices"].get(key)
        if price is not None:
            free.append({"price": key, "sku": price["sku"], "usagetype": price["attributes"].get("usagetype"),
                         "allowance": price["dimensions"][0]["endRange"], "unit": price["unit"]})
    return {"fixed": fixed, "scenarios": scenarios, "free_tiers_not_subtracted": free}


def newest_snapshot(directory) -> Path:
    found = sorted(p for p in Path(directory).glob("prices-*.json") if SNAPSHOT.fullmatch(p.name))
    if not found:
        raise EstimateError(f"no prices-<region>-<YYYYMMDD>.json in {directory}; run snapshot.py")
    return found[-1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table(result: dict, prices_name: str, quantities: dict) -> str:
    out = [f"Monthly cost of infra/aws in {quantities['region']}, {quantities['hours_per_month']} h, "
           f"USD list prices from {prices_name}; not deployed", ""]

    def rows(lines):
        for line in lines:
            usd = line["usd"] if line["usd"] is not None else "not priced"
            detail = f"{line.get('sku', '')} {line.get('rate', '')}".strip()
            if not detail and line["status"] == "no charge":
                detail = "no charge"
            quantity = f"{line['quantity']:,}" if isinstance(line["quantity"], int) else f"{line['quantity']:.6g}"
            out.append(f"  {line['id']:<28} {quantity:>14} {line['unit']:<13} {usd:>12}  {detail}")
            if line["status"] == "not priced":
                out.append(f"  {'':<28} {line.get('formula', line['reason'])}")

    out.append("Fixed, billed whether or not anything runs")
    rows(result["fixed"]["lines"])
    out.append(f"Fixed monthly total: {result['fixed']['total_usd']} USD")
    for name, scenario in result["scenarios"].items():
        out += ["", f"Scenario {name}: {scenario['about']}"]
        rows(scenario["lines"])
        out.append(f"Usage total {scenario['total_usd']} USD; with the fixed lines {scenario['with_fixed_usd']} USD; "
                   f"not priced: {', '.join(scenario['not_priced']) or 'none'}")
    free = ", ".join(f"{f['allowance']} {f['unit']} ({f['usagetype']})" for f in result["free_tiers_not_subtracted"])
    out += ["", f"Account-wide free tiers, not subtracted: {free}"]
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Estimate the AWS design's monthly cost, or verify its quantities.")
    parser.add_argument("command", nargs="?", choices=("estimate", "verify"), default="estimate")
    parser.add_argument("--tf-dir", type=Path, default=HERE.parent)
    parser.add_argument("--quantities", type=Path, default=HERE / "quantities.json")
    parser.add_argument("--prices", type=Path, help="default: the newest snapshot beside this script")
    parser.add_argument("--evidence-dir", type=Path, default=LAB / "evidence")
    args = parser.parse_args(argv)
    try:
        quantities = json.loads(args.quantities.read_text(encoding="utf-8"))
        problems = verify(args.tf_dir, quantities)
        if problems:
            print("quantities.json does not match the Terraform:", *problems, sep="\n  ", file=sys.stderr)
            return 1
        if args.command == "verify":
            print(f"quantities.json matches the Terraform in {args.tf_dir}")
            return 0
        prices_path = args.prices or newest_snapshot(HERE)
        prices = json.loads(prices_path.read_text(encoding="utf-8"))
        result = estimate(quantities, prices, args.tf_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        record = {
            "kind": "cost-estimate",
            "generated": stamp,
            "note": "List prices only. The design is not deployed; no AWS account or credentials were used.",
            "region": quantities["region"],
            "currency": "USD",
            "hours_per_month": quantities["hours_per_month"],
            "prices_file": prices_path.name,
            "prices_sha256": sha256(prices_path),
            "quantities_sha256": sha256(args.quantities),
            "terraform_sha256": {p.name: sha256(p) for p in sorted(args.tf_dir.glob("*.tf"))},
            "price_list": {
                "source": prices["source"],
                "index_publicationDate": prices["index_publicationDate"],
                "retrieved": prices["retrieved"],
                "offers": {name: {k: meta[k] for k in ("version", "publicationDate", "sha256")}
                           for name, meta in prices["offers"].items()},
            },
            "assumes": quantities["assumes"],
            "formulas": FORMULAS,
            **result,
            "not_in_design": quantities["not_in_design"],
        }
        args.evidence_dir.mkdir(parents=True, exist_ok=True)
        target = args.evidence_dir / f"cost-estimate-{stamp}.json"
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    except (EstimateError, OSError, ValueError, KeyError) as exc:
        print(f"estimate failed: {exc}", file=sys.stderr)
        return 1
    print(table(result, prices_path.name, quantities))
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
