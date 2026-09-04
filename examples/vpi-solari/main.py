"""VPI on Solari: collect retail prices from a Cloudflare-gated storefront.

This is a production collector from the Venezuela Price Index (vpindex.org),
a daily goods-price index built from scraped retail prices. Six of its seven
retail sources are plain HTTP. The seventh, Automercados Plaza's, is behind a
site-wide Cloudflare Managed Challenge that only a real Chrome passes, and it
previously needed a visible Chrome window on a laptop. This example runs that
collector on a Solari stealth browser instead: no laptop, no display, a
replay per run.

Two commands:

  python main.py discover harina --n 5
      Search the store for a term and print the product identities found.
      This is how a panel is seeded.

  python main.py panel panel.json --out rows.json
      Re-fetch a fixed panel of product ids and write today's rows to a file.
      This is the daily job.

Both print a run receipt (counts, latency, session id, replay URL when
`--record` is set). Prices are written to `--out`, never printed, unless you
pass `--show-prices`. The index sells the data; the code is public.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from plazas_parser import (
    DEFAULT_BRANCH,
    looks_like_challenge,
    parse_product_page,
    parse_search_page,
    product_url,
    search_url,
)
from solari_fetch import SolariFetcher, ensure_profile

MAX_SEARCH_PAGES = 3


async def discover(fetcher: SolariFetcher, term: str, n: int, branch: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for page in range(1, MAX_SEARCH_PAGES + 1):
        if len(found) >= n:
            break
        res = await fetcher.fetch(search_url(term, page, branch))
        if not res.ok:
            print(f"[discover] term={term!r} page={page} status={res.status} challenged={res.challenged} -> stop", file=sys.stderr)
            break
        tiles = parse_search_page(res.html, category=term)
        if not tiles:
            break
        found.extend(t.as_dict() for t in tiles)
    return found[:n]


async def fetch_panel(fetcher: SolariFetcher, items: list[dict[str, Any]], branch: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        entity_id = str(item.get("source_id") or "")
        if not entity_id:
            continue
        res = await fetcher.fetch(product_url(entity_id, branch))
        if not res.ok or looks_like_challenge(res.html):
            continue  # MISSING for today; the caller measures dropout
        row = parse_product_page(res.html, entity_id)
        if row is not None:
            rows.append(row.as_dict())
    return rows


def _receipt_line(receipt: dict[str, Any], extra: dict[str, Any]) -> str:
    out = {**extra, **receipt}
    return json.dumps(out, ensure_ascii=False)


async def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("SOLARI_API_KEY", "")
    if not api_key:
        print("SOLARI_API_KEY is not set (see .env.sample)", file=sys.stderr)
        return 2

    profile_id = None
    if args.profile:
        profile_id = await ensure_profile(api_key, args.profile)

    t0 = time.monotonic()
    async with SolariFetcher(
        api_key,
        proxy_country=args.proxy,
        recording=args.record,
        profile_id=profile_id,
        save_profile=bool(profile_id),
        spacing_sec=args.spacing,
    ) as fetcher:
        if args.command == "discover":
            results = await discover(fetcher, args.term, args.n, args.branch)
            kind = "candidates"
        else:
            panel = json.loads(Path(args.panel).read_text())
            items = panel["items"] if isinstance(panel, dict) else panel
            items = [it for it in items if it.get("source", "plazas") == "plazas"]
            results = await fetch_panel(fetcher, items, args.branch)
            kind = "rows"
        receipt = await fetcher.finish()

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    summary = {
        "command": args.command,
        "date": time.strftime("%Y-%m-%d"),
        "branch": args.branch,
        kind: len(results),
        "wall_ms": int((time.monotonic() - t0) * 1000),
    }
    print(_receipt_line(receipt.as_dict(), summary))

    if args.show_prices or (args.command == "discover" and not args.out):
        shown = results if args.show_prices else [
            {k: v for k, v in r.items() if k not in ("price_usd", "list_price_usd")} for r in results
        ]
        print(json.dumps(shown, indent=2, ensure_ascii=False))
    return 0 if results else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--proxy", default=os.environ.get("SOLARI_PROXY_COUNTRY", ""), help="residential egress country; off by default (not needed for this site, see README)")
    p.add_argument("--branch", default=DEFAULT_BRANCH, help="store branch subdomain to pin")
    p.add_argument("--record", action="store_true", help="record the session and print a replay URL")
    p.add_argument("--profile", default="", help="Solari profile name to carry clearance cookies across runs")
    p.add_argument("--spacing", type=float, default=2.0, help="seconds between page loads")
    p.add_argument("--out", default="", help="write results (with prices) to this JSON file")
    p.add_argument("--show-prices", action="store_true", help="print prices to stdout (off by default)")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover", help="search the store and list product identities")
    d.add_argument("term")
    d.add_argument("--n", type=int, default=5)
    f = sub.add_parser("panel", help="re-fetch a fixed panel of product ids")
    f.add_argument("panel", help="JSON: a list of {source_id} or {items: [...]}")
    return p


if __name__ == "__main__":
    sys.exit(asyncio.run(run(build_parser().parse_args())))
