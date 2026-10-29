"""Save a few real answers from the Copernicus catalogue as test fixtures.

Run it once by hand. The tests then read the files in tests/fixtures/,
so they never need the network. Each file keeps the exact request, the
time it was captured and a fingerprint of the raw answer, so anyone can
check where the data came from.
"""

import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

SEARCH_URL = "https://catalogue.dataspace.copernicus.eu/stac/search"
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
PAUSE_SECONDS = 2.0  # the server answered 429 after about 10 fast calls
TIMEOUT = (5, 60)  # seconds to connect, seconds to read

BORNHEIM = [6.966, 50.761, 6.994, 50.779]  # west, south, east, north
SEASON = "2026-04-01T00:00:00Z/2026-09-30T23:59:59Z"
SORT = [
    {"field": "properties.datetime", "direction": "asc"},
    {"field": "id", "direction": "asc"},
]
BASE = {
    "collections": ["sentinel-2-l2a"],
    "bbox": BORNHEIM,
    "datetime": SEASON,
    "limit": 3,
    "sortby": SORT,
}
CLOUD_MAX_30 = {
    "filter-lang": "cql2-json",
    "filter": {"op": "<=", "args": [{"property": "eo:cloud_cover"}, 30]},
}


def capture(session, name, body, url=SEARCH_URL):
    time.sleep(PAUSE_SECONDS)
    captured_at = datetime.now(timezone.utc).isoformat()
    response = session.post(url, json=body, timeout=TIMEOUT, allow_redirects=False)  # like the client
    record = {
        "captured_at": captured_at,
        "request": {"method": "POST", "url": url, "body": body},
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "sha256": hashlib.sha256(response.content).hexdigest(),
        "response": response.json(),
    }
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    scenes = len(record["response"].get("features", []))
    print(f"{name}: HTTP {response.status_code}, {scenes} scenes, {len(response.content) / 1024:.0f} KB")
    return record


def summarise():
    pages = [
        json.loads((OUT_DIR / f"{name}.json").read_text(encoding="utf-8"))["response"]
        for name in ("search_page1", "search_page2")
    ]
    scenes = [feature for page in pages for feature in page.get("features", [])]
    ids = [feature.get("id") for feature in scenes]
    clouds = [f["properties"].get("eo:cloud_cover") for f in scenes if f["properties"].get("eo:cloud_cover") is not None]
    print(f"\nscenes in page 1 and 2: {len(scenes)}, unique ids: {len(set(ids))}")
    print("geometry types:", dict(Counter((f.get("geometry") or {}).get("type") for f in scenes)))
    print("tiles:", dict(Counter(f["properties"].get("grid:code") for f in scenes)))
    print("cloud cover min and max:", min(clouds), max(clouds))
    for label, key in (("top-level fields", None), ("properties", "properties"), ("assets", "assets")):
        counts = Counter(k for f in scenes for k in (f if key is None else f.get(key, {})))
        print(f"\n{label}, scenes that have it out of {len(scenes)}:")
        for field, n in sorted(counts.items()):
            print(f"  {n}/{len(scenes)}  {field}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        session.trust_env = False  # no proxies, CA bundles or .netrc logins from the environment
        page1 = capture(session, "search_page1", BASE)
        next_link = next(link for link in page1["response"]["links"] if link.get("rel") == "next")
        capture(session, "search_page2", next_link["body"], url=next_link["href"])
        capture(session, "search_cloud_max30", {**BASE, **CLOUD_MAX_30})
        capture(session, "search_empty", {**BASE, "datetime": "2010-01-01T00:00:00Z/2010-01-31T23:59:59Z"})
        capture(session, "error_unknown_collection", {**BASE, "collections": ["SENTINEL-2"]})
    summarise()


if __name__ == "__main__":
    main()
