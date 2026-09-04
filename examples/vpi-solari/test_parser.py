"""Offline tests for the parser. Run: python test_parser.py

The fixtures are fabricated. They copy only the Magento 2 markup SHAPES the
live site renders (search grid tile, product page, Cloudflare interstitial),
with made-up product ids, SKUs, names and prices, so the parser can be
checked without a browser, an API key, or any real market data.
"""

from __future__ import annotations

from plazas_parser import (
    looks_like_challenge,
    parse_product_page,
    parse_search_page,
    product_url,
    search_url,
)

SEARCH_HTML = """
<html><body>
<ol class="products list items product-items">
  <li class="item product product-item id-9001">
    <div class="product-item-info">
      <a href="/producto-de-prueba-a-500g.html" class="product photo">
        <img src="x.jpg" alt="PRODUCTO DE PRUEBA A 500G">
      </a>
      <strong class="product name product-item-name">
        <a class="product-item-link"><span class="mst-search__highlight">PRODUCTO</span> DE PRUEBA A 500G</a>
      </strong>
      <div class="price-box price-final_price" data-role="priceBox" data-product-id="9001">
        <span class="price-container"><span data-price-type="finalPrice" data-price-amount="1.23" class="price-wrapper">$1.23</span></span>
      </div>
      <form data-role="tocart-form" data-product-sku="TST-0001"><button>Agregar</button></form>
    </div>
  </li>
  <li class="item product product-item id-9002">
    <img alt="PRODUCTO DE PRUEBA B 250G" src="y.jpg">
    <div class="price-box" data-product-id="9002">
      <span data-price-type="oldPrice" data-price-amount="4.59"></span>
      <span data-price-type="finalPrice" data-price-amount="3.79"></span>
    </div>
    <form data-product-sku="TST-0002"></form>
  </li>
  <li class="item product product-item">
    <div class="price-box" data-product-id="9003"><span data-price-type="finalPrice" data-price-amount="0"></span></div>
  </li>
</ol>
</body></html>
"""

PRODUCT_HTML = """
<html><body>
<div class="column main">
  <div class="product-info-main">
    <h1 class="page-title"><span>PRODUCTO DE PRUEBA B 250G</span></h1>
    <div class="product-info-price">
      <div class="price-box" data-product-id="9002">
        <span data-price-type="oldPrice" data-price-amount="4.59"></span>
        <span data-price-type="finalPrice" data-price-amount="3.79"></span>
      </div>
      <div class="stock available"><span>Disponible</span></div>
    </div>
  </div>
  <div class="related">
    <div class="price-box"><span data-price-type="finalPrice" data-price-amount="99.99"></span></div>
  </div>
</div>
</body></html>
"""

CHALLENGE_HTML = """
<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div id="challenge-running">Checking your browser</div>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body></html>
"""


def test_search_grid() -> None:
    tiles = parse_search_page(SEARCH_HTML, category="prueba")
    assert [t.source_id for t in tiles] == ["9001", "9002", "9003"], tiles
    a, b, c = tiles
    assert a.name == "PRODUCTO DE PRUEBA A 500G"
    assert a.sku == "TST-0001"
    assert a.price_usd == 1.23 and a.list_price_usd == 1.23
    assert b.price_usd == 3.79 and b.list_price_usd == 4.59  # on promo
    assert c.price_usd is None  # zero price is never a price


def test_product_page_scoped_to_main() -> None:
    row = parse_product_page(PRODUCT_HTML, "9002")
    assert row is not None
    assert row.price_usd == 3.79 and row.list_price_usd == 4.59
    assert row.stock == 1
    assert parse_product_page("<html><body>nothing</body></html>", "1") is None


def test_challenge_detection() -> None:
    assert looks_like_challenge(CHALLENGE_HTML)
    assert looks_like_challenge("", title="Just a moment...")
    assert not looks_like_challenge(PRODUCT_HTML, title="PRODUCTO DE PRUEBA B 250G")


def test_urls() -> None:
    assert search_url("harina pan") == "https://vallearriba.elplazas.com/catalogsearch/result/?q=harina+pan"
    assert search_url("arroz", page=2).endswith("?q=arroz&p=2")
    assert product_url("9001") == "https://vallearriba.elplazas.com/catalog/product/view/id/9001/"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("all parser tests passed")


# ---- listing pages (fabricated markup, same shapes as the live store) ----
from listing import Category, listing_url, parse_categories, parse_toolbar  # noqa: E402

HOME_NAV_HTML = """
<nav class="navigation"><ul>
  <li class="level0"><a href="https://vallearriba.elplazas.com/depto-uno.html"><span>DEPTO UNO (113)</span></a>
    <ul><li class="level1"><a href="https://vallearriba.elplazas.com/depto-uno/sub.html">SUB</a></li></ul></li>
  <li class="level0"><a href="https://vallearriba.elplazas.com/depto-dos.html"><span>DEPTO DOS</span></a></li>
  <li class="level0"><a href="https://vallearriba.elplazas.com/depto-uno.html"><span>DEPTO UNO (113)</span></a></li>
</ul></nav>
"""


def test_categories_and_toolbar() -> None:
    cats = parse_categories(HOME_NAV_HTML)
    assert [(c.name, c.nav_count) for c in cats] == [("DEPTO UNO", 113), ("DEPTO DOS", None)], cats
    assert listing_url(cats[0].url, 1) == cats[0].url
    assert listing_url(cats[0].url, 3).endswith("depto-uno.html?p=3")
    assert parse_toolbar('<p class="toolbar-amount"><span>Artículos</span> 16 - 30 <span>de</span> 106</p>') == (16, 30, 106)
    assert parse_toolbar('<p class="toolbar-amount">7 Artículos</p>') == (1, 7, 7)
    assert parse_toolbar("<p></p>") == (None, None, None)


if __name__ == "__main__":
    test_categories_and_toolbar()
    print("ok  test_categories_and_toolbar")
