"""A Solari-backed page fetcher for sites behind a bot-defense challenge.

One stealth session per run, reused for every page in that run. The session is
created with the three knobs that matter for a Cloudflare-gated storefront:

  stealth=True   a real headful Chrome on real hardware, so the TLS/JA3
                 fingerprint is a browser's, not a Python client's
  captcha=True   Solari clears Turnstile / managed challenges for us
  proxy=...      sticky residential egress, so the clearance cookie the site
                 hands out keeps matching the IP that earned it

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

CHALLENGE_WAIT_SEC = 45
CHALLENGE_POLL_SEC = 1.5


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
    total_ms: int = 0
    replay_url: str = ""
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
        proxy_country: str = "us",
        sticky_label: str = "vpi-plazas",
        recording: bool = False,
        profile_id: Optional[str] = None,
        save_profile: bool = False,
        spacing_sec: float = 2.0,
        goto_timeout_ms: int = 30_000,
    ) -> None:
        self._solari = Solari(api_key=api_key)
        self._proxy = ProxyRequest(country=proxy_country, session=sticky_label, session_duration=30)
        self._recording = recording
        self._profile_id = profile_id
        self._save_profile = save_profile
        self._spacing = spacing_sec
        self._goto_timeout = goto_timeout_ms
        self._browser: Any = None
        self._page: Any = None
        self._last_fetch_at = 0.0
        self.receipt: Optional[RunReceipt] = None

    async def __aenter__(self) -> "SolariFetcher":
        self._browser = await self._solari.launch(
            stealth=True,
            captcha=True,
            proxy=self._proxy,
            recording=self._recording,
            profile_id=self._profile_id,
            retries=1,
        )
        self.receipt = RunReceipt(
            session_id=self._browser.id,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            proxy_country=(self._browser.proxy.country if self._browser.proxy else ""),
            proxy_tier=(self._browser.proxy.tier or "") if self._browser.proxy else "",
            profile_id=self._profile_id or "",
        )
        self._page = await self._browser.new_page()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.finish()

    @property
    def session_id(self) -> str:
        return self._browser.id if self._browser else ""

    async def fetch(self, url: str) -> FetchResult:
        assert self._page is not None and self.receipt is not None
        # Polite spacing between page loads, measured from the previous fetch.
        wait = self._spacing - (time.monotonic() - self._last_fetch_at)
        if wait > 0:
            await asyncio.sleep(wait)
        t0 = time.monotonic()
        status: Optional[int] = None
        challenged = False
        html = ""
        try:
            resp = await self._page.goto(url, wait_until="domcontentloaded", timeout=self._goto_timeout)
            status = resp.status if resp is not None else None
            html, title = await self._read_document()
            # Solari solves the challenge in-page; the document swaps itself
            # for the real one when it is done. Poll until that happens.
            deadline = time.monotonic() + CHALLENGE_WAIT_SEC
            while _looks_like_challenge(html, title) and time.monotonic() < deadline:
                challenged = True
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
        """Save the profile if asked, release the session, collect the replay URL."""
        assert self.receipt is not None
        if self._browser is None:
            return self.receipt
        session_id = self._browser.id
        try:
            if self._save_profile and self._profile_id:
                ctx = self._browser.contexts()[0]
                state = await ctx.storage_state()
                await self._solari.profiles.save(self._profile_id, state)
        except Exception as e:  # noqa: BLE001
            self.receipt.errors.append(f"profile save: {type(e).__name__}: {e}"[:300])
        try:
            await self._browser.close()
        finally:
            self._browser = None
            self._page = None
        if self._recording:
            self.receipt.replay_url = await self._replay_url(session_id)
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
