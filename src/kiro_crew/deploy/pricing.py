"""Live AWS unit prices for deploy cost estimates.

The what-if cost buckets shown on artifact cards and the deploy console use
LIVE unit prices from the AWS Pricing API when the deploy profile can reach it,
falling back to a hardcoded price table -- an estimate labelled "not a bill"
must never block on a pricing lookup.

Design constraints:
- The Pricing API is only served from us-east-1/eu-central-1/ap-south-1; we
  always query us-east-1 regardless of the deploy region (prices are filtered
  by the deploy region's location name).
- Responses are large stringified JSON documents; every extraction step is
  wrapped so a shape drift degrades to the fallback price, never a 500.
- Results are cached per (profile, region) for 24h -- unit prices change on
  the order of months.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass

from kiro_crew.deploy import engine

logger = logging.getLogger(__name__)

_PRICING_API_REGION = "us-east-1"
_CACHE_TTL_SECS = 24 * 3600
_CACHE: dict[tuple[str, str], tuple[float, "UnitPrices"]] = {}
_CACHE_MAX = 64

_REGION_LOCATIONS = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)",
    "eu-central-1": "EU (Frankfurt)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-south-1": "Asia Pacific (Mumbai)",
}


@dataclass(frozen=True)
class UnitPrices:
    """USD unit prices used by the what-if estimate buckets."""

    s3_gb_month: float = 0.023
    cf_per_10k_requests: float = 0.0075
    cf_gb_egress: float = 0.085
    lambda_per_request: float = 0.0000002
    lambda_gb_second: float = 0.0000166667
    source: str = "fallback"

    def to_dict(self) -> dict:
        return asdict(self)


_FALLBACK = UnitPrices()


def _first_on_demand_usd(price_list_entry: str) -> float | None:
    """Extract the first OnDemand USD price from one PriceList document."""
    doc = json.loads(price_list_entry)
    terms = doc["terms"]["OnDemand"]
    term = next(iter(terms.values()))
    dim = next(iter(term["priceDimensions"].values()))
    usd = dim["pricePerUnit"]["USD"]
    value = float(usd)
    return value if value > 0 else None


def _query_price(profile: str, service_code: str, filters: list[tuple[str, str]]) -> float | None:
    """One Pricing API lookup -> first positive OnDemand USD price, or None."""
    args = [
        "pricing", "get-products",
        "--service-code", service_code,
        "--region", _PRICING_API_REGION,
        "--max-results", "1",
        "--output", "json",
    ]
    for field, value in filters:
        args += ["--filters", f"Type=TERM_MATCH,Field={field},Value={value}"]
    try:
        rc, out, _ = engine.run_aws(args, profile, 20)
        if rc != 0 or not out:
            return None
        entries = json.loads(out).get("PriceList") or []
        if not entries:
            return None
        return _first_on_demand_usd(entries[0])
    except Exception:  # noqa: BLE001 -- any shape drift degrades to fallback
        logger.debug("pricing lookup failed for %s", service_code, exc_info=True)
        return None


def get_unit_prices(profile: str, region: str) -> UnitPrices:
    """Live unit prices for the deploy region, falling back per-field.

    Every field that the Pricing API cannot supply keeps its fallback value;
    ``source`` is ``live`` only when at least one live price was obtained.
    """
    key = (profile or "", region or "")
    now = time.time()
    cached = _CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    location = _REGION_LOCATIONS.get(region, "")
    values = _FALLBACK.to_dict()
    values.pop("source")
    live = 0

    if location:
        s3 = _query_price(profile, "AmazonS3", [
            ("location", location),
            ("storageClass", "General Purpose"),
            ("volumeType", "Standard"),
        ])
        if s3 is not None:
            values["s3_gb_month"] = s3
            live += 1

        lam_req = _query_price(profile, "AWSLambda", [
            ("location", location),
            ("group", "AWS-Lambda-Requests"),
        ])
        if lam_req is not None:
            values["lambda_per_request"] = lam_req
            live += 1

        lam_gbs = _query_price(profile, "AWSLambda", [
            ("location", location),
            ("group", "AWS-Lambda-Duration"),
        ])
        if lam_gbs is not None:
            values["lambda_gb_second"] = lam_gbs
            live += 1

    # CloudFront prices by geographic zone, not AWS region.
    cf_req = _query_price(profile, "AmazonCloudFront", [
        ("productFamily", "Request"),
        ("location", "United States"),
        ("requestType", "HTTPS"),
    ])
    if cf_req is not None:
        values["cf_per_10k_requests"] = cf_req * 10000
        live += 1

    cf_gb = _query_price(profile, "AmazonCloudFront", [
        ("productFamily", "Data Transfer"),
        ("location", "United States"),
    ])
    if cf_gb is not None:
        values["cf_gb_egress"] = cf_gb
        live += 1

    prices = UnitPrices(**values, source="live" if live else "fallback")
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (now + _CACHE_TTL_SECS, prices)
    return prices
