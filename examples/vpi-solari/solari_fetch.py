"""A Solari-backed page fetcher for sites behind a bot-defense challenge.

One stealth session per run, reused for every page in that run. What was
verified live against a Cloudflare Managed Challenge (2026-09-04):

  stealth=True   a real headful Chrome on real hardware. Required: the
                 default browser is served the interstitial forever.
  captcha=True   Solari's managed solving. Left on, but on its own it did
                 not clear this site's Turnstile within 45 s in any run.
  the click      what actually clears it: after the interstitial renders,
                 click the Turnstile checkbox inside Cloudflare's iframe
                 (`challenges.cloudflare.com`). Cleared in ~15 s every time.
  proxy=None     the default. On the Starter plan, residential/mobile
                 egress either failed at the tunnel (ERR_TUNNEL_CONNECTION_
                 FAILED) or reported the same datacenter IP as no proxy,
                 and the challenge cleared without one. `proxy_country` is
                 still accepted for plans where egress works.

Optionally the run is recorded (`recording=True`) so a replay URL comes back
with the receipt, and a Solari profile can carry the clearance cookies from
one day's run into the next so the challenge is paid once, not daily.

Every fetch returns the page HTML plus a small status record. Nothing in this
file knows what a price is.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from solari_browser import ProxyRequest, Solari
from solari_browser.errors import SolariError

CHALLENGE_WAIT_SEC = 75
CHALLENGE_POLL_SEC = 1.5
CLICK_AFTER_SEC = 6      # let the widget render before the first click
CLICK_RETRY_SEC = 12     # click again if the interstitial is still there
EXPIRY_MARGIN_SEC = 45   # relaunch before the session's expires_at, not after


def _parse_iso(ts: str) -> Optional[float]:
    """ISO-8601 UTC ('...Z') -> epoch seconds, or None."""
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _browser_gone(e: Exception) -> bool:
    msg = f"{type(e).__name__}: {e}"
    return any(k in msg for k in ("TargetClosedError", "Browser closed", "has been closed", "Target closed", "frame was detached", "ERR_ABORTED"))


def _looks_like_challenge(html: str, title: str) -> bool:
    t = title.lower()
    h = html[:20000].lower()
    return "just a moment" in t or "just a moment" in h or "cf-chl" in h or "challenge-platform" in h


@dataclass
class FetchResult:
    url: str
    status: Optional[int]
    html: str
    elapsed_ms: int
    challenged: bool  # a challenge page was seen before the real document
    ok: bool


@dataclass
class RunReceipt:
    """What a run leaves behind. Counts and identifiers only, never content."""

    session_id: str
    started_at: str
    finished_at: str = ""
    proxy_country: str = ""
    proxy_tier: str = ""
    fetches: int = 0
    ok: int = 0
    challenged: int = 0
    clicks: int = 0  # Turnstile checkbox clicks issued
    relaunches: int = 0  # sessions opened after the first one died or neared expiry
    session_ids: list[str] = field(default_factory=list)
    total_ms: int = 0
    replay_url: str = ""
    replay_urls: list[str] = field(default_factory=list)
    profile_id: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SolariFetcher:
    """Async context manager. `async with SolariFetcher(...) as f: await f.fetch(url)`."""

    def __init__(
        self,
        api_key: str,
        *,
        proxy_country: str = "",
        sticky_label: str = "",
        recording: bool = False,
        profile_id: Optional[str] = None,
        save_profile: bool = False,
        spacing_sec: float = 2.0,
        goto_timeout_ms: int = 30_000,
    ) -> None:
        self._solari = Solari(api_key=api_key)
        self._proxy = (
            ProxyRequest(country=proxy_country, session=sticky_label or None) if proxy_country else None
        )
        self._recording = recording
        self._profile_id = profile_id
        self._save_profile = save_profile
        self._spacing = spacing_sec
        self._goto_timeout = goto_timeout_ms
        self._browser: Any = None
        self._page: Any = None
        self._last_fetch_at = 0.0
        self._expires_at: Optional[float] = None
        self.receipt: Optional[RunReceipt] = None

    async def _launch(self) -> None:
        self._browser = await self._solari.launch(
            stealth=True,
            captcha=True,
            proxy=self._proxy,
            recording=self._recording,
            profile_id=self._profile_id,
            retries=1,
        )
        self._page = await self._browser.new_page()
        self._expires_at = _parse_iso(self._browser.expires_at)
        if self.receipt is not None:
            self.receipt.session_ids.append(self._browser.id)

    async def __aenter__(self) -> "SolariFetcher":
        self.receipt = RunReceipt(session_id="", started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), profile_id=self._profile_id or "")
        await self._launch()
        self.receipt.session_id = self._browser.id
        self.receipt.proxy_country = self._browser.proxy.country if self._browser.proxy else ""
        self.receipt.proxy_tier = (self._browser.proxy.tier or "") if self._browser.proxy else ""
        return self

    async def _release(self, collect_replay: bool) -> None:
        """Close the current browser; optionally fetch its replay URL."""
        if self._browser is None:
            return
        sid = self._browser.id
        try:
            await self._browser.close()
        except Exception:  # noqa: BLE001 - already dead is fine
            pass
        self._browser = None
        self._page = None
        if collect_replay and self._recording and self.receipt is not None:
            url = await self._replay_url(sid)
            if url:
                self.receipt.replay_urls.append(url)

    async def _relaunch(self, why: str) -> None:
        """Sessions have a lifetime (`expires_at`). A crawl longer than that
        must carry on in a fresh session: same run, same receipt, one more
        challenge to click through."""
        assert self.receipt is not None
        self.receipt.relaunches += 1
        self.receipt.errors.append(f"relaunch #{self.receipt.relaunches}: {why}"[:200])
        await self._release(collect_replay=True)
        await self._launch()

    def _near_expiry(self) -> bool:
        return self._expires_at is not None and (self._expires_at - time.time()) < EXPIRY_MARGIN_SEC

    async def __aexit__(self, *_exc: Any) -> None:
        await self.finish()

    @property
    def session_id(self) -> str:
        return self._browser.id if self._browser else ""

    async def fetch(self, url: str) -> FetchResult:
        assert self.receipt is not None
        # Polite spacing between page loads, measured from the previous fetch.
        wait = self._spacing - (time.monotonic() - self._last_fetch_at)
        if wait > 0:
            await asyncio.sleep(wait)
        if self._near_expiry():
            await self._relaunch("session near expiry")
        t0 = time.monotonic()
        status: Optional[int] = None
        challenged = False
        html = ""
        try:
            try:
                resp = await self._page.goto(url, wait_until="domcontentloaded", timeout=self._goto_timeout)
            except Exception as e:  # noqa: BLE001
                if not _browser_gone(e):
                    raise
                await self._relaunch(f"{type(e).__name__}: {e}".splitlines()[0][:120])
                resp = await self._page.goto(url, wait_until="domcontentloaded", timeout=self._goto_timeout)
            status = resp.status if resp is not None else None
            html, title = await self._read_document()
            # Solari solves the challenge in-page; the document swaps itself
            # for the real one when it is done. Poll until that happens.
            deadline = time.monotonic() + CHALLENGE_WAIT_SEC
            next_click = time.monotonic() + CLICK_AFTER_SEC
            while _looks_like_challenge(html, title) and time.monotonic() < deadline:
                challenged = True
                if time.monotonic() >= next_click:
                    await self._click_turnstile()
                    next_click = time.monotonic() + CLICK_RETRY_SEC
                await asyncio.sleep(CHALLENGE_POLL_SEC)
                html, title = await self._read_document()
            if challenged and not _looks_like_challenge(html, title):
                status = 200  # the interstitial's 403 is not the document we have now
        except Exception as e:  # noqa: BLE001 - one bad page must not end the run
            self.receipt.errors.append(f"{url}: {type(e).__name__}: {e}"[:300])
        finally:
            self._last_fetch_at = time.monotonic()

        elapsed = int((time.monotonic() - t0) * 1000)
        ok = status == 200 and bool(html) and not _looks_like_challenge(html, "")
        self.receipt.fetches += 1
        self.receipt.ok += int(ok)
        self.receipt.challenged += int(challenged)
        self.receipt.total_ms += elapsed
        return FetchResult(url=url, status=status, html=html, elapsed_ms=elapsed, challenged=challenged, ok=ok)

    async def _click_turnstile(self) -> None:
        """Click the checkbox in Cloudflare's Turnstile iframe, if present.
        The widget lives in a cross-origin frame; clicking its bounding box
        through the top-level mouse is what a person does, and what cleared
        the managed challenge in every live run."""
        assert self.receipt is not None
        try:
            for frame in self._page.frames:
                if "challenges.cloudflare.com" in (frame.url or ""):
                    el = await frame.frame_element()
                    box = await el.bounding_box()
                    if box:
                        await self._page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                        self.receipt.clicks += 1
                        return
        except Exception as e:  # noqa: BLE001 - the frame can vanish mid-click when it clears
            self.receipt.errors.append(f"turnstile click: {type(e).__name__}: {e}"[:200])

    async def _read_document(self) -> tuple[str, str]:
        """Read html + title, tolerating the challenge page replacing itself
        mid-read (patchright raises when the execution context is destroyed)."""
        for attempt in range(3):
            try:
                return await self._page.content(), await self._page.title()
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                await asyncio.sleep(CHALLENGE_POLL_SEC)
        return "", ""  # unreachable

    async def finish(self) -> RunReceipt:
        """Save the profile if asked, release the session, collect the replay URL(s)."""
        assert self.receipt is not None
        if self._browser is None:
            return self.receipt
        try:
            if self._save_profile and self._profile_id and self._browser.contexts():
                state = await self._browser.contexts()[0].storage_state()
                await self._solari.profiles.save(self._profile_id, state)
        except Exception as e:  # noqa: BLE001
            self.receipt.errors.append(f"profile save: {type(e).__name__}: {e}"[:300])
        await self._release(collect_replay=True)
        if self.receipt.replay_urls:
            self.receipt.replay_url = self.receipt.replay_urls[-1]
        await self._solari.close()
        self.receipt.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return self.receipt

    async def _replay_url(self, session_id: str) -> str:
        # The recording uploads after release; the first polls usually 404.
        for _ in range(10):
            await asyncio.sleep(3)
            try:
                r = await self._solari.sessions.get_replay_url(session_id)
                return r.url
            except SolariError as err:
                if err.status == 404:
                    continue
                self.receipt.errors.append(f"replay: {err}"[:300])  # type: ignore[union-attr]
                return ""
        return ""


async def ensure_profile(api_key: str, name: str) -> str:
    """Return the id of the profile called `name`, creating it if needed."""
    solari = Solari(api_key=api_key)
    try:
        for p in await solari.profiles.list():
            if p.name == name:
                return p.id
        return (await solari.profiles.create(name)).id
    finally:
        await solari.close()
