#!/usr/bin/env python3
"""probe_coverage.py — does OpenStreetMap actually know about these places?

Run this BEFORE building anything on a new category. It answers the only
question that matters for generalising Shum-AI beyond restaurants: how many
venues of this kind exist in OSM's Hong Kong data, and how many of them carry
a phone number the agent could dial.

Restaurants set the bar: 1,199 named, 156 callable, about one in five. A
category that comes back with 12 named and 2 callable cannot carry a demo, and
finding that out now costs four minutes instead of four hours.

    python3 probe_coverage.py                 # every category
    python3 probe_coverage.py court_sport     # one
    python3 probe_coverage.py --json          # machine-readable, for a report

Needs working network. The Overpass public endpoint is slow under load and
rate-limits aggressively, so this sleeps between categories on purpose.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

import domains
from places import normalise_phone

# One box over the whole urban territory. Wider than places.py uses at runtime
# because this is a census, not a search, and a slow honest count beats a fast
# misleading one.
HK_BBOX = (22.19, 113.83, 22.56, 114.41)

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
TIMEOUT = 90


def fetch(query: str) -> list[dict]:
    last = None
    for endpoint in ENDPOINTS:
        try:
            req = urllib.request.Request(
                endpoint,
                data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
                headers={"User-Agent": "shumai-coverage-probe/0.1"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8")).get("elements", [])
        except Exception as exc:  # noqa: BLE001 - report, try the mirror
            last = exc
            print(f"  [{endpoint.split('/')[2]}] {type(exc).__name__}: {exc}")
    raise SystemExit(f"all Overpass endpoints failed: {last}")


def probe(category: domains.Category) -> dict:
    query = domains.build_selector_query(category, HK_BBOX, limit=2000)
    elements = fetch(query)

    named = callable_ = 0
    websites = 0
    descriptors: Counter = Counter()
    samples: list[dict] = []

    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        if not name:
            continue
        named += 1

        phone = None
        for key in ("phone", "contact:phone", "phone:HK", "contact:mobile", "mobile"):
            phone = normalise_phone(tags.get(key))
            if phone:
                break
        if phone:
            callable_ += 1
        if tags.get("website") or tags.get("contact:website"):
            websites += 1

        value = (tags.get(category.descriptor_tag) or "").strip()
        if value:
            descriptors[value] += 1
        if phone and len(samples) < 5:
            samples.append({"name": name, "phone": phone,
                            category.descriptor_tag: value or None})

    return {
        "category": category.key,
        "selectors": category.osm_selectors,
        "elements_returned": len(elements),
        "named": named,
        "callable": callable_,
        "callable_pct": round(100 * callable_ / named, 1) if named else 0.0,
        "with_website": websites,
        "descriptor_tag": category.descriptor_tag,
        "descriptor_coverage_pct": (
            round(100 * sum(descriptors.values()) / named, 1) if named else 0.0),
        "top_descriptors": descriptors.most_common(8),
        "samples": samples,
        "books_by": category.books_by,
    }


def verdict(result: dict) -> str:
    """A demo needs roughly twenty dialable venues to survive a poll, a veto
    and a restaurant that does not pick up. Below that, the category is a
    research problem, not a feature."""
    if result["callable"] >= 40:
        return "GOOD - enough to demo and to re-roll"
    if result["callable"] >= 20:
        return "THIN - workable, but one bad call ruins it"
    if result["callable"] >= 5:
        return "WEAK - needs a second data source"
    return "DEAD - OSM does not know this category in Hong Kong"


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    wanted = [a for a in argv[1:] if not a.startswith("-")]
    keys = wanted or list(domains.CATEGORIES)

    results = []
    for i, key in enumerate(keys):
        category = domains.get(key)
        if not as_json:
            print(f"\n=== {category.plural} ({category.key}) ===")
            print(f"  books by: {category.books_by}")
        result = probe(category)
        result["verdict"] = verdict(result)
        results.append(result)

        if not as_json:
            print(f"  named venues:      {result['named']}")
            print(f"  with a phone:      {result['callable']} "
                  f"({result['callable_pct']}%)")
            print(f"  with a website:    {result['with_website']}")
            print(f"  {category.descriptor_tag} tagged:    "
                  f"{result['descriptor_coverage_pct']}%")
            if result["top_descriptors"]:
                print(f"  most common:       {result['top_descriptors'][:5]}")
            for s in result["samples"]:
                print(f"    e.g. {s['name']}  {s['phone']}")
            print(f"  VERDICT: {result['verdict']}")

        if i < len(keys) - 1:
            time.sleep(5)  # be a good citizen of a free public endpoint

    if as_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print("\n--- summary ---")
        for r in results:
            print(f"  {r['category']:<12} {r['named']:>5} named  "
                  f"{r['callable']:>4} callable   {r['verdict']}")
        print("\nRestaurants, for reference: 1199 named, 156 callable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
