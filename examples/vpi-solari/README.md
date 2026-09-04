# VPI on Solari: a Cloudflare-gated retailer, collected from the cloud

A production collector from the [Venezuela Price Index](https://vpindex.org),
a daily goods-price index built from scraped retail prices in the one economy
where official statistics are least trustworthy. The index has run every day
since 2026-07-03 across six retail chains, all of them over plain HTTP.

The seventh chain, Automercados Plaza's, was never in the daily run. Its whole
site sits behind a Cloudflare Managed Challenge that gates on the browser's
TLS fingerprint, not just a cookie. In the recon that found this:

- plain `requests` got 403 on every path, `robots.txt` included
- a bundled headless Chromium looped on the challenge forever
- the `cf_clearance` cookie, replayed into a plain HTTP client, was re-challenged
- real Chrome passed, but only headful, with a visible window on a laptop

So the collector existed, worked, and could not be scheduled: it needed a
human's screen. This example moves it to a Solari stealth browser. Same parser,
no display, no laptop, one replay per run. First live run: challenge cleared in
13 seconds, 15 product tiles parsed, replay saved.

## What it does

```
python main.py crawl --out rows.json            # daily: every department, every tile, every price
python main.py discover harina --n 5            # search the store, list product ids
python main.py panel panel.json --out rows.json # spot check: one product page per id
```

The daily job is the crawl. A product page is one price per load; a listing
page is fifteen, and the store's page size is locked at fifteen no matter what
`product_list_limit` asks for. So the collector walks each department's
listing pages with `?p=N`, reads the toolbar's "1 - 15 de 106" to know when a
department is done, and reads every tile. Nine departments, about 2,800 SKUs
by the nav counts, under 200 page loads, roughly ten minutes of browser time.
That is the whole store every day, not a sample of it.

All three commands print a run receipt: session id, page count, how many
pages hit a challenge, how many Turnstile clicks were issued, latency, and a
replay URL when `--record` is set. Prices go to `--out`, never to stdout,
unless you pass `--show-prices`. The index sells the data and publishes the code.

```
python main.py --record --profile vpi-plazas crawl --out rows.json
```

`--profile` stores the session's cookies in a Solari profile at the end of the
run and attaches them next time. In practice a new session is challenged again
regardless, so this costs nothing and saves nothing today; it is left in for
when clearance survives the session.

## How it is put together

| File | Role |
| --- | --- |
| `plazas_parser.py` | Pure functions over HTML: product tiles to candidates, product page to a price row, challenge detection. No network. |
| `listing.py` | The catalog walk: department links from the nav, `?p=N` pagination, toolbar totals, dedup across departments. Takes any object with `fetch(url)`. |
| `solari_fetch.py` | One stealth session per run (`stealth`, `captcha`, optional `recording`, `profile_id`, `proxy`). `fetch(url)` waits for the challenge to clear, clicking the Turnstile checkbox in Cloudflare's iframe when it renders, and returns the document plus a status record. |
| `main.py` | The three commands and the receipt. |
| `test_parser.py` | Offline fixtures for both parsers. `python test_parser.py` |

What actually clears the challenge, verified live on 2026-09-04:

```python
browser = await solari.launch(stealth=True, captcha=True)   # real headful Chrome; no proxy
page = await browser.new_page()
await page.goto(url)                                        # 403, "Just a moment..."
# ...wait for the widget, then click the checkbox inside Cloudflare's iframe:
for frame in page.frames:
    if "challenges.cloudflare.com" in frame.url:
        box = await (await frame.frame_element()).bounding_box()
        await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
# ~15 s later the document is the store, and the same tab keeps the clearance.
```

Three things learned the hard way, in case they save you an afternoon:

- The default (non-stealth) browser is served the interstitial forever.
  Stealth is required, and it clears without any proxy.
- `captcha=True` alone did not clear this site's Turnstile within 45 s in any
  run. The click did, every time. Both are left on.
- On the Starter plan, residential and mobile proxies either failed at the
  tunnel (`ERR_TUNNEL_CONNECTION_FAILED`) or reported the same datacenter IP
  as no proxy. `--proxy` is accepted but off by default.

## Run

```bash
cd examples/vpi-solari
pip install -r requirements.txt
cp .env.sample .env && edit .env       # SOLARI_API_KEY from https://console.getsolari.com
export $(grep -v '^#' .env | xargs)
python test_parser.py                  # offline, no key needed
python main.py discover harina --n 5
```

## Notes

- The store branch is pinned by subdomain, so the site does not need a
  Venezuelan IP to serve prices (and Venezuela is not a Solari proxy country).
- Listing pages carry 15 tiles each and the store ignores `product_list_limit`.
  The nav's per-department counts run low; the listing toolbar's total is
  the number to trust.
- Tiles do not render a stock badge, so stock is unknown from the crawl.
  The product page does render it, which is what `panel` is for.
- `FetchResult.ok` is false for anything that is not a clean document: a
  challenge that never cleared, a 404, a timeout. The caller counts those as
  missing for the day and measures dropout; a missing price is never filled in.
- Prices are USD as the store publishes them. No bolivar leg is invented.

Built with Claude Code for the Pinetree Research intern challenge. The rest of the index lives in a private repo; this example
is the one piece that needed a browser.
