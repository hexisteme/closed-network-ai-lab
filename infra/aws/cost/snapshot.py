"""Trim the public AWS Price List to the SKUs the cost estimate uses, for one Region.

Reads the bulk Price List, which needs no account or credentials: the offer index, each offer's
Region index, then the Region's offer file. Offer files can be tens of megabytes, so they go to a
scratch directory outside the repository; only the trimmed snapshot is written here, as
prices-<region>-<YYYYMMDD>.json (the UTC date the index was retrieved). Each price key in
quantities.json must match exactly one product with one on-demand term, or nothing is written.
The Bedrock offers are recorded too, without prices, so the reason Bedrock stays unpriced can be
checked: they name models, not the model IDs the Terraform takes.

Downloaded files are untrusted data, parsed as JSON only. Run with python3 -I:
  python3 -I infra/aws/cost/snapshot.py --scratch <dir outside the repo> [--reuse]
--reuse reads files already in <dir> (index.json, dl-<offer>/region_index.json and offer.json)
instead of downloading them again. Standard library only.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest

HERE = Path(__file__).resolve().parent
BASE = "https://pricing.us-east-1.amazonaws.com"
INDEX_URL = BASE + "/offers/v1.0/aws/index.json"


class SnapshotError(Exception):
    """The Price List did not give one unambiguous price for a key."""


def fetch(url: str, target: Path, reuse: bool) -> dict:
    """Download url to target (or reuse it); return its parsed JSON, SHA-256 and retrieval time."""
    if not url.startswith(BASE + "/"):
        raise SnapshotError(f"not a Price List URL: {url}")
    if not (reuse and target.is_file()):
        target.parent.mkdir(parents=True, exist_ok=True)
        with urlrequest.urlopen(url, timeout=300) as response:
            target.write_bytes(response.read())
    raw = target.read_bytes()
    retrieved = datetime.fromtimestamp(target.stat().st_mtime, timezone.utc)
    return {"data": json.loads(raw), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "retrieved": retrieved}


def load_offer(scratch: Path, index: dict, offer: str, region: str, reuse: bool) -> dict:
    entry = index["offers"].get(offer)
    if entry is None:
        raise SnapshotError(f"offer {offer} is not in the index")
    region_index_url = BASE + entry["currentRegionIndexUrl"]
    region_index = fetch(region_index_url, scratch / f"dl-{offer}" / "region_index.json", reuse)
    regional = region_index["data"]["regions"].get(region)
    if regional is None:
        raise SnapshotError(f"offer {offer} has no {region} file")
    offer_url = BASE + regional["currentVersionUrl"]
    got = fetch(offer_url, scratch / f"dl-{offer}" / "offer.json", reuse)
    data = got["data"]
    return {
        "data": data,
        "meta": {
            "region_index_url": region_index_url,
            "offer_url": offer_url,
            "version": data.get("version"),
            "publicationDate": data.get("publicationDate"),
            "sha256": got["sha256"],
            "bytes": got["bytes"],
        },
    }


def select_price(key: str, selector: dict, offer: dict) -> dict:
    """The one product whose attributes match the selector, with its on-demand price dimensions."""
    data = offer["data"]
    matches = [
        (sku, product) for sku, product in data["products"].items()
        if all(product.get("attributes", {}).get(name) == value for name, value in selector["match"].items())
    ]
    if len(matches) != 1:
        raise SnapshotError(f"{key}: {len(matches)} products match {selector['match']} in {selector['offer']}")
    sku, product = matches[0]
    terms = data["terms"].get("OnDemand", {}).get(sku, {})
    if len(terms) != 1:
        raise SnapshotError(f"{key}: SKU {sku} has {len(terms)} on-demand terms")
    term = next(iter(terms.values()))
    dimensions = sorted(
        (
            {
                "rateCode": dim["rateCode"],
                "beginRange": dim.get("beginRange", "0"),
                "endRange": dim.get("endRange", "Inf"),
                "unit": dim["unit"],
                "usd": dim["pricePerUnit"]["USD"],
                "description": dim.get("description", ""),
            }
            for dim in term["priceDimensions"].values()
        ),
        key=lambda dim: float(dim["beginRange"]),
    )
    units = {dim["unit"] for dim in dimensions}
    if len(units) != 1:
        raise SnapshotError(f"{key}: SKU {sku} prices in more than one unit: {sorted(units)}")
    return {
        "offer": selector["offer"],
        "sku": sku,
        "productFamily": product.get("productFamily"),
        "attributes": {k: v for k, v in product["attributes"].items() if k not in ("servicecode", "servicename", "location", "locationType")},
        "offerTermCode": term["offerTermCode"],
        "effectiveDate": term["effectiveDate"],
        "unit": units.pop(),
        "dimensions": dimensions,
    }


def describe_unused_offer(offer: dict) -> dict:
    """An offer read but not priced: its identity and which attributes its products carry."""
    products = offer["data"]["products"]
    names = sorted({name for product in products.values() for name in product.get("attributes", {})})
    return dict(offer["meta"], products=len(products), attribute_names=names)


def build_snapshot(quantities: dict, scratch: Path, reuse: bool) -> dict:
    region = quantities["region"]
    index = fetch(INDEX_URL, scratch / "index.json", reuse)
    offers = {}
    for selector in quantities["prices"].values():
        if selector["offer"] not in offers:
            offers[selector["offer"]] = load_offer(scratch, index["data"], selector["offer"], region, reuse)
    prices = {key: select_price(key, selector, offers[selector["offer"]]) for key, selector in quantities["prices"].items()}
    checked = {
        name: describe_unused_offer(load_offer(scratch, index["data"], name, region, reuse))
        for name in quantities["bedrock"]["offers_checked"]
    }
    return {
        "source": INDEX_URL,
        "index_publicationDate": index["data"].get("publicationDate"),
        "index_sha256": index["sha256"],
        "retrieved": index["retrieved"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": region,
        "offers": {name: offer["meta"] for name, offer in sorted(offers.items())},
        "prices": prices,
        "offers_checked_without_prices": checked,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", required=True, type=Path, help="directory outside the repository for the downloads")
    parser.add_argument("--reuse", action="store_true", help="read files already downloaded to --scratch")
    parser.add_argument("--quantities", type=Path, default=HERE / "quantities.json")
    args = parser.parse_args(argv)
    scratch = args.scratch.resolve()
    if scratch == HERE.parents[2] or HERE.parents[2] in scratch.parents:
        print("--scratch must be outside the repository", file=sys.stderr)
        return 2
    quantities = json.loads(args.quantities.read_text(encoding="utf-8"))
    try:
        snapshot = build_snapshot(quantities, scratch, args.reuse)
    except (SnapshotError, OSError, ValueError, KeyError) as exc:
        print(f"snapshot failed: {exc}", file=sys.stderr)
        return 1
    stamp = snapshot["retrieved"][:10].replace("-", "")
    target = HERE / f"prices-{snapshot['region']}-{stamp}.json"
    target.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(snapshot['prices'])} prices from {len(snapshot['offers'])} offers -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
