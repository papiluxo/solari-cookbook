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
no display, no laptop, one replay per run.

## What it does

```
python main.py discover harina --n 5          # seed: search the store, list product ids
python main.py panel panel.json --out rows.json   # daily: re-fetch a fixed panel
```

Both print a run receipt: session id, egress country, page count, how many
pages hit a challenge, latency, and a replay URL when `--record` is set.
Prices go to `--out`, never to stdout, unless you pass `--show-prices`.
The index sells the data and publishes the code.

```
python main.py --record --profile vpi-plazas panel panel.json --out rows.json
```

`--profile` stores the session's cookies in a Solari profile at the end of the
run and attaches them next time, so the challenge is paid once, not daily.
This is the cloud version of the persistent Chrome profile the laptop used.

## How it is put together

| File | Role |
| --- | --- |
| `plazas_parser.py` | Pure functions over HTML: search grid to candidates, product page to a price row, challenge detection. No network. |
| `solari_fetch.py` | One stealth session per run (`stealth`, `captcha`, sticky residential `proxy`, optional `recording` and `profile_id`). `fetch(url)` waits for the challenge to clear and returns the document plus a status record. |
| `main.py` | The two commands and the receipt. |
| `test_parser.py` | Offline fixtures for the parser. `python test_parser.py` |

The three Solari knobs that make this work together:

```python
browser = await solari.launch(
    stealth=True,                      # real Chrome fingerprint, so the TLS gate passes
    captcha=True,                      # Solari clears the managed challenge in-page
    proxy=ProxyRequest(country="us", session="vpi-plazas", session_duration=30),
)                                      # sticky egress, so the clearance keeps matching the IP
```

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

- Venezuela is not a Solari proxy country. The store branch is pinned by
  subdomain, so the site does not need a Venezuelan IP to serve prices.
- One product per page load; there is no batch endpoint. A 60-item panel is
  about 60 page loads at 2-second spacing, a few minutes of stealth time.
- `FetchResult.ok` is false for anything that is not a clean document: a
  challenge that never cleared, a 404, a timeout. The caller counts those as
  missing for the day and measures dropout; a missing price is never filled in.
- Prices are USD as the store publishes them. No bolivar leg is invented.

Built with Claude Code for the Pinetree Research intern challenge. The rest of the index lives in a private repo; this example
is the one piece that needed a browser.
