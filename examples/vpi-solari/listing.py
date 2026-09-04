"""Full-catalog crawl through category listing pages.

A product page is one price per load. A listing page is fifteen. The store
locks `product_list_limit` (36, 100 and 300 were all ignored; 15 tiles came
back every time), but `?p=N` pagination works and the toolbar says how many
items the category holds, so the whole department tree can be walked in a
few hundred loads and every price read from the tiles the parser already
understands.

Seen live 2026-09-04 (ad-hoc recon, no saved receipt): 9 top-level departments, ~2,800 SKUs by the nav
counts, 15 tiles per page, toolbar text "Artículos 1 - 15 de 106".
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from parsel import Selector

from plazas_parser import Candidate, base_url, parse_search_page

PAGE_SIZE = 15
TOOLBAR_RANGE = re.compile(r"(\d+)\s*-\s*(\d+)\s*de\s*(\d+)")
TOOLBAR_SINGLE = re.compile(r"^\s*(\d+)\s*Art")
NAV_COUNT = re.compile(r"\((\d+)\)\s*$")


@dataclass
class Category:
    name: str
    url: str
    nav_count: Optional[int]


@dataclass
class CrawlStats:
    categories: int = 0
    pages: int = 0
    tiles: int = 0
    unique: int = 0
    failed_pages: int = 0
    per_category: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_categories(home_html: str) -> list[Category]:
    """Top-level departments from the store's main navigation."""
    sel = Selector(text=home_html)
    out: list[Category] = []
    seen: set[str] = set()
    for a in sel.css("nav.navigation li.level0 > a"):
        href = a.attrib.get("href", "").strip()
        raw = " ".join(a.css("::text").getall()).strip()
        if not href or href in seen:
            continue
        seen.add(href)
        m = NAV_COUNT.search(raw)
        name = NAV_COUNT.sub("", raw).strip()
        out.append(Category(name=name, url=href, nav_count=int(m.group(1)) if m else None))
    return out


def listing_url(category_url: str, page: int = 1) -> str:
    if page <= 1:
        return category_url
    sep = "&" if "?" in category_url else "?"
    return f"{category_url}{sep}p={page}"


def parse_toolbar(html: str) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """(first, last, total) from 'Artículos 1 - 15 de 106'; (1, n, n) for 'n Artículos'."""
    text = " ".join(Selector(text=html).css(".toolbar-amount ::text").getall())
    m = TOOLBAR_RANGE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = TOOLBAR_SINGLE.search(text)
    if m:
        n = int(m.group(1))
        return 1, n, n
    return None, None, None


def parse_listing(html: str, category: str) -> tuple[list[Candidate], Optional[int], Optional[int]]:
    """Tiles on this page, the last item index shown, and the category total."""
    tiles = parse_search_page(html, category=category)
    _, last, total = parse_toolbar(html)
    return tiles, last, total


async def crawl(
    fetcher: Any,
    categories: Optional[list[Category]] = None,
    *,
    max_pages_per_category: int = 200,
    log: Any = None,
) -> tuple[dict[str, dict[str, Any]], CrawlStats]:
    """Walk every listing page of every department. Returns {entity_id: tile}
    deduplicated across departments (first department wins) plus stats.
    `fetcher` is anything with `async fetch(url) -> FetchResult`."""
    stats = CrawlStats()
    if categories is None:
        home = await fetcher.fetch(base_url() + "/")
        categories = parse_categories(home.html) if home.ok else []
    stats.categories = len(categories)
    found: dict[str, dict[str, Any]] = {}
    for cat in categories:
        seen_here = 0
        for page in range(1, max_pages_per_category + 1):
            res = await fetcher.fetch(listing_url(cat.url, page))
            stats.pages += 1
            if not res.ok:
                stats.failed_pages += 1
                if log:
                    log(f"[crawl] {cat.name} p{page}: status={res.status} challenged={res.challenged} -> stop category")
                break
            tiles, last, total = parse_listing(res.html, category=cat.name)
            if not tiles:
                break
            stats.tiles += len(tiles)
            for t in tiles:
                d = t.as_dict()
                if d["source_id"] not in found:
                    found[d["source_id"]] = d
                    seen_here += 1
            if total is not None and last is not None and last >= total:
                break
            if len(tiles) < PAGE_SIZE and total is None:
                break
        stats.per_category[cat.name] = seen_here
        if log:
            log(f"[crawl] {cat.name}: {seen_here} new ids (nav says {cat.nav_count})")
    stats.unique = len(found)
    return found, stats


def estimate_loads(categories: list[Category]) -> int:
    return sum(-(-(c.nav_count or 0) // PAGE_SIZE) for c in categories) + 1


if __name__ == "__main__":
    print("module; use main.py crawl", time.strftime("%Y-%m-%d"))
