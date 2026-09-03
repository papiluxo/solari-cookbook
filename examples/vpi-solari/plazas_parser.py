"""Pure HTML parsing for a Magento 2 storefront (Automercados Plaza's, Venezuela).

No network, no browser, no Solari in this file. It takes the HTML a Solari
browser already fetched and turns it into rows. Keeping the parser separate is
what lets the collector be tested offline against saved fixtures, and swapped
onto any fetcher (Solari today, a local Chrome yesterday).

Why this retailer needs a real browser at all: the whole site sits behind a
Cloudflare Managed Challenge that gates on the TLS fingerprint, not just the
cookie. Plain HTTP clients get 403 on every path, robots.txt included. Only a
real Chrome network stack passes, which is exactly what `solari_fetch.py`
provides from the cloud.

Markup facts (verified live, 2026-08-03; re-verify if selectors go stale):
  search grid    GET /catalogsearch/result/?q={term}[&p={page}]
                 li.product-item  ->  [data-product-id] (Magento entity_id,
                 the stable identity key), form[data-product-sku],
                 img[alt] (the name; anchor text is split by a highlight
                 span), [data-price-type=finalPrice][data-price-amount],
                 [data-price-type=oldPrice][data-price-amount] when on promo.
  product page   GET /catalog/product/view/id/{entity_id}/  (slug-independent)
                 scoped to .product-info-main; stock badge .stock.available /
                 .stock.unavailable (a word, not a count).
  currency       USD. The page publishes no bolivar leg, so none is invented.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any, Optional

from parsel import Selector

SOURCE = "plazas"

# The chain runs ~15 branches, each on its own subdomain. One branch is pinned
# so the panel never silently switches shelves.
DEFAULT_BRANCH = "vallearriba"


def base_url(branch: str = DEFAULT_BRANCH) -> str:
    return f"https://{branch}.elplazas.com"


def search_url(term: str, page: int = 1, branch: str = DEFAULT_BRANCH) -> str:
    params: dict[str, str] = {"q": term}
    if page > 1:
        params["p"] = str(page)
    return f"{base_url(branch)}/catalogsearch/result/?{urllib.parse.urlencode(params)}"


def product_url(entity_id: str, branch: str = DEFAULT_BRANCH) -> str:
    return f"{base_url(branch)}/catalog/product/view/id/{entity_id}/"


def looks_like_challenge(html: str, title: str = "") -> bool:
    """True when the document is Cloudflare's interstitial, not the store."""
    t = (title or "").lower()
    h = html[:20000].lower()
    return (
        "just a moment" in t
        or "just a moment" in h
        or "cf-chl" in h
        or "challenge-platform" in h
        or 'id="challenge-running"' in h
    )


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


@dataclass
class Candidate:
    """One product tile from a search results grid."""

    source: str
    source_id: str
    sku: Optional[str]
    name: Optional[str]
    category: str
    price_usd: Optional[float]
    list_price_usd: Optional[float]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PriceRow:
    """One re-fetched panel item from its product page."""

    source: str
    source_id: str
    price_usd: float
    list_price_usd: float
    stock: Optional[int]  # 1 available, 0 unavailable, None when no badge

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_search_page(html: str, category: str) -> list[Candidate]:
    sel = Selector(text=html)
    out: list[Candidate] = []
    for tile in sel.css("li.product-item"):
        entity_id = tile.css("[data-product-id]::attr(data-product-id)").get()
        if not entity_id:
            continue
        price = _to_float(tile.css('[data-price-type="finalPrice"]::attr(data-price-amount)').get())
        old = _to_float(tile.css('[data-price-type="oldPrice"]::attr(data-price-amount)').get())
        out.append(
            Candidate(
                source=SOURCE,
                source_id=str(entity_id),
                sku=tile.css("form[data-product-sku]::attr(data-product-sku)").get(),
                name=tile.css("img::attr(alt)").get(),
                category=category,
                price_usd=price,
                list_price_usd=old if old else price,
            )
        )
    return out


def parse_product_page(html: str, entity_id: str) -> Optional[PriceRow]:
    """None means MISSING: no price on the page. Never returns a zero price."""
    sel = Selector(text=html)
    main = sel.css(".product-info-main")
    if not main:
        return None
    price = _to_float(main.css('[data-price-type="finalPrice"]::attr(data-price-amount)').get())
    if price is None:
        return None
    old = _to_float(main.css('[data-price-type="oldPrice"]::attr(data-price-amount)').get())
    stock: Optional[int] = None
    badge = main.css(".stock")
    if badge:
        classes = badge[0].attrib.get("class", "")
        if "unavailable" in classes:
            stock = 0
        elif "available" in classes:
            stock = 1
    return PriceRow(
        source=SOURCE,
        source_id=str(entity_id),
        price_usd=price,
        list_price_usd=old if old else price,
        stock=stock,
    )
