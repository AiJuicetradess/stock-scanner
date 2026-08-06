"""Scan Public.com's full equity instrument list for 52-week lows via the API.

Primary path (authenticated):
  1. GET all EQUITY instruments (BUY_AND_SELL)
  2. For each symbol, YEAR bars → 52w high/low vs last close
  3. Keep names within ``threshold_pct`` of the 52-week low
  4. Optionally enrich hits with company description from public.com stock page

Fallback (no API key):
  Prebuilt screener collection at public.com/markets/52-week-low
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from .config import PublicCredentials
from .filters import HitFilterConfig, filter_hits
from .models import LowHit, ScanState

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_COLLECTION_URL = (
    "https://prod-api.154310543964.hellopublic.com/static/"
    "asset-collection-metrics/MIN_52_WEEKS_PRICE_USD/metric-collections.json"
)
MARKETS_PAGE = "https://public.com/markets/52-week-low"
STOCK_PAGE = "https://public.com/stocks/{symbol}"

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)

OnUpdate = Optional[Callable[[ScanState], None]]


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _s(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class ScanConfig:
    """Knobs for a universe or screener scan."""

    mode: str = "universe"  # universe | screener
    threshold_pct: float = 2.0  # within N% of 52w low
    concurrency: int = 15
    limit: Optional[int] = None  # cap universe size (for tests)
    min_price: float = 1.0
    min_bars: int = 60  # require ~3 months of history
    enrich_hits: bool = True
    enrich_concurrency: int = 4
    progress_every: int = 25  # UI log cadence
    # Post-scan filters (name/mcap/industry need enrich)
    exclude_etf: bool = False
    exclude_spac: bool = False
    exclude_preferred: bool = False
    min_market_cap: Optional[float] = None
    industries: Optional[list[str]] = None  # include substrings vs industry

    def hit_filters(self) -> HitFilterConfig:
        return HitFilterConfig(
            exclude_etf=self.exclude_etf,
            exclude_spac=self.exclude_spac,
            exclude_preferred=self.exclude_preferred,
            min_market_cap=self.min_market_cap,
            industries=list(self.industries or []),
            industry_include=True,
        )


class PublicLowScanner:
    """Authenticated full-universe 52-week low scanner (+ screener fallback)."""

    def __init__(self, config: Optional[ScanConfig] = None) -> None:
        self.config = config or ScanConfig()
        self._http: Optional[httpx.AsyncClient] = None
        self._creds: Optional[PublicCredentials] = None

    async def __aenter__(self) -> "PublicLowScanner":
        self._http = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        try:
            self._creds = PublicCredentials.from_env()
        except RuntimeError:
            self._creds = None
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("scanner must be used as an async context manager")
        return self._http

    # ── public entry ──────────────────────────────────────────────────────

    async def scan(self, state: ScanState, *, on_update: OnUpdate = None) -> ScanState:
        mode = (self.config.mode or "universe").lower()
        if mode == "screener":
            return await self._scan_screener(state, on_update=on_update)
        if self._creds is None:
            state.log("⚠ No API credentials — falling back to Public screener list")
            if on_update:
                on_update(state)
            return await self._scan_screener(state, on_update=on_update)
        return await self._scan_universe(state, on_update=on_update)

    # ── universe (API) ────────────────────────────────────────────────────

    async def _scan_universe(self, state: ScanState, *, on_update: OnUpdate) -> ScanState:
        from public_api_sdk import (
            ApiKeyAuthConfig,
            AsyncPublicApiClient,
            AsyncPublicApiClientConfiguration,
            BarPeriod,
            InstrumentType,
            InstrumentsRequest,
            OrderInstrument,
            TradingPermission,
        )

        assert self._creds is not None
        cfg = self.config

        def bump(phase: str, message: str) -> None:
            state.phase = phase
            state.message = message
            if on_update:
                on_update(state)

        bump("universe", "Authenticating with Public API…")
        state.log("→ Public API auth (instrument list + YEAR bars)")

        try:
            async with AsyncPublicApiClient(
                auth_config=ApiKeyAuthConfig(
                    api_secret_key=self._creds.api_secret_key,
                    validity_minutes=120,
                ),
                config=AsyncPublicApiClientConfiguration(
                    default_account_number=self._creds.account_id
                ),
            ) as api:
                bump("universe", "Loading full equity instrument list…")
                # get_all_instruments was flaky on async in probes — use thread for sync call
                symbols = await asyncio.to_thread(self._load_universe_sync)
                symbols = sorted(set(symbols))
                if cfg.limit is not None and cfg.limit > 0:
                    symbols = symbols[: cfg.limit]

                state.total = len(symbols)
                state.universe_size = len(symbols)
                state.log(f"✓ Universe loaded: {len(symbols):,} equities (BUY_AND_SELL)")
                bump("scan", f"Scanning 0/{len(symbols):,} for 52-week lows…")

                if not symbols:
                    state.finished = True
                    bump("done", "Empty instrument list")
                    return state

                sem = asyncio.Semaphore(max(1, cfg.concurrency))
                lock = asyncio.Lock()
                completed = 0
                errors = 0
                hits: list[LowHit] = []
                t0 = time.monotonic()
                # throttle UI updates
                last_ui = 0.0

                async def check_one(sym: str) -> None:
                    nonlocal completed, errors, last_ui
                    async with sem:
                        hit, err = await self._evaluate_symbol_bars(api, sym, BarPeriod)
                    async with lock:
                        completed += 1
                        state.current_index = completed
                        state.current_symbol = sym
                        state.scanned = completed
                        if err:
                            errors += 1
                        state.error_count = errors
                        if hit is not None:
                            hits.append(hit)
                            # keep hits sorted by proximity to low
                            hits.sort(
                                key=lambda h: (
                                    h.pct_from_52w_low
                                    if h.pct_from_52w_low is not None
                                    else 999.0
                                )
                            )
                            state.hits = list(hits)
                            state.log(
                                f"★ HIT {hit.symbol}  {hit.price}  "
                                f"from low {hit.pct_from_52w_low:.2f}%  "
                                f"52w {hit.low_52w}–{hit.high_52w}"
                                if hit.pct_from_52w_low is not None
                                else f"★ HIT {hit.symbol}"
                            )

                        now = time.monotonic()
                        should_ui = (
                            completed == 1
                            or completed == state.total
                            or completed % cfg.progress_every == 0
                            or hit is not None
                            or (now - last_ui) >= 0.4
                        )
                        if should_ui:
                            last_ui = now
                            elapsed = max(now - t0, 0.001)
                            rate = completed / elapsed
                            remaining = state.total - completed
                            eta_s = remaining / rate if rate > 0 else 0
                            state.scan_rate = rate
                            state.message = (
                                f"Scanning {completed:,}/{state.total:,}  ·  "
                                f"{len(hits)} hits  ·  "
                                f"{rate:.0f}/s  ·  ETA {eta_s/60:.1f}m  ·  {sym}"
                            )
                            if completed % max(cfg.progress_every * 4, 1) == 0:
                                state.log(
                                    f"… {completed:,}/{state.total:,}  "
                                    f"hits={len(hits)}  err={errors}  "
                                    f"{rate:.0f}/s"
                                )
                            if on_update:
                                on_update(state)

                await asyncio.gather(*(check_one(s) for s in symbols))

                state.hits = hits
                state.current_symbol = ""
                state.log(
                    f"✓ Bar scan done — {len(hits)} within {cfg.threshold_pct:g}% of 52w low "
                    f"({errors} errors / {completed:,} scanned)"
                )

                # Live quotes for hits (batch), then drop any that left the zone
                if hits:
                    bump("quotes", f"Refreshing quotes for {len(hits)} hits…")
                    await self._refresh_hit_quotes(
                        api, hits, OrderInstrument, InstrumentType
                    )
                    kept = [h for h in hits if self._still_near_low(h)]
                    dropped = len(hits) - len(kept)
                    if dropped:
                        state.log(
                            f"· Dropped {dropped} after live quote "
                            f"(no longer ≤{cfg.threshold_pct:g}% from low)"
                        )
                    hits = kept
                    hits.sort(
                        key=lambda h: (
                            h.pct_from_52w_low
                            if h.pct_from_52w_low is not None
                            else 999.0
                        )
                    )
                    state.hits = list(hits)
                    if on_update:
                        on_update(state)

                # Optional description enrich via public stock pages.
                # Filters that inspect name/desc/mcap/industry force enrich on.
                filters = cfg.hit_filters()
                need_enrich = cfg.enrich_hits or (
                    bool(hits) and filters.needs_enrich()
                )
                if need_enrich and hits:
                    if not cfg.enrich_hits and filters.needs_enrich():
                        state.log(
                            "· Enabling enrich (required for active name/mcap/industry filters)"
                        )
                    bump("enrich", f"Enriching {len(hits)} hits with company details…")
                    await self._enrich_hits(hits, state, on_update)
                    state.hits = list(hits)

                hits = self._apply_hit_filters(hits, state, on_update)
                state.hits = list(hits)

        except Exception as exc:  # noqa: BLE001
            state.error = str(exc)
            state.log(f"✗ Universe scan failed: {exc}")
            state.finished = True
            bump("error", state.error)
            return state

        state.finished = True
        bump(
            "done",
            f"Done — {len(state.hits)} names within {cfg.threshold_pct:g}% of 52-week low "
            f"(scanned {state.total:,})",
        )
        return state

    def _load_universe_sync(self) -> list[str]:
        from public_api_sdk import (
            ApiKeyAuthConfig,
            InstrumentType,
            InstrumentsRequest,
            PublicApiClient,
            PublicApiClientConfiguration,
            TradingPermission,
        )

        assert self._creds is not None
        client = PublicApiClient(
            ApiKeyAuthConfig(
                api_secret_key=self._creds.api_secret_key,
                validity_minutes=60,
            ),
            config=PublicApiClientConfiguration(
                default_account_number=self._creds.account_id
            ),
        )
        try:
            resp = client.get_all_instruments(
                InstrumentsRequest(
                    type_filter=[InstrumentType.EQUITY],
                    trading_filter=[TradingPermission.BUY_AND_SELL],
                )
            )
            out: list[str] = []
            for row in resp.instruments or []:
                inst = row.instrument
                if inst and inst.symbol:
                    out.append(str(inst.symbol).upper())
            return out
        finally:
            client.close()

    async def _evaluate_symbol_bars(
        self,
        api: Any,
        symbol: str,
        BarPeriod: Any,
    ) -> tuple[Optional[LowHit], bool]:
        """Return (hit_or_None, had_error)."""
        cfg = self.config
        try:
            bars = await api.get_bars(symbol, BarPeriod.YEAR)
        except Exception:
            # one retry for transient auth/rate issues
            try:
                await asyncio.sleep(0.15)
                bars = await api.get_bars(symbol, BarPeriod.YEAR)
            except Exception:
                return None, True

        series = []
        rm = getattr(bars, "regular_market", None)
        if rm and getattr(rm, "bars", None):
            series = rm.bars
        if len(series) < cfg.min_bars:
            return None, False

        lows: list[float] = []
        highs: list[float] = []
        last_close: Optional[float] = None
        last_low: Optional[float] = None
        for bar in series:
            lo = _f(bar.low)
            hi = _f(bar.high)
            cl = _f(bar.close)
            if lo is not None:
                lows.append(lo)
            if hi is not None:
                highs.append(hi)
            if cl is not None:
                last_close = cl
            if lo is not None:
                last_low = lo

        if not lows or last_close is None:
            return None, False

        low_52w = min(lows)
        high_52w = max(highs) if highs else None
        if low_52w <= 0:
            return None, False
        if last_close < cfg.min_price:
            return None, False

        # "at" 52w low: last close within threshold, OR today's low printed the 52w low
        pct_from = ((last_close - low_52w) / low_52w) * 100.0
        made_new_low = last_low is not None and last_low <= low_52w * 1.001
        if pct_from > cfg.threshold_pct and not made_new_low:
            return None, False

        return (
            LowHit(
                symbol=symbol,
                price=last_close,
                low_52w=low_52w,
                high_52w=high_52w,
                low_today=last_low,
                name=symbol,
                enriched=False,
            ),
            False,
        )

    async def _refresh_hit_quotes(
        self,
        api: Any,
        hits: list[LowHit],
        OrderInstrument: Any,
        InstrumentType: Any,
    ) -> None:
        # batch quotes in chunks of 50
        chunk = 50
        by_sym = {h.symbol: h for h in hits}
        symbols = list(by_sym.keys())
        for i in range(0, len(symbols), chunk):
            batch = symbols[i : i + chunk]
            instruments = [
                OrderInstrument(symbol=s, type=InstrumentType.EQUITY) for s in batch
            ]
            try:
                quotes = await api.get_quotes(instruments)
            except Exception:
                continue
            for q in quotes or []:
                sym = getattr(getattr(q, "instrument", None), "symbol", None)
                if not sym or sym not in by_sym:
                    continue
                hit = by_sym[sym]
                # Never clobber a real bar close with a zero/empty quote
                last = _f(q.last)
                if last is not None and last > 0:
                    hit.price = last
                prev = _f(q.previous_close)
                if prev is not None and prev > 0:
                    hit.previous_close = prev
                vol = _f(q.volume)
                if vol is not None and vol > 0:
                    hit.volume = vol
                odc = getattr(q, "one_day_change", None)
                if odc is not None:
                    chg = _f(getattr(odc, "percent_change", None))
                    usd = _f(getattr(odc, "change", None))
                    if chg is not None:
                        hit.change_pct = chg
                    if usd is not None:
                        hit.change_usd = usd

    def _still_near_low(self, hit: LowHit) -> bool:
        if hit.price is None or hit.low_52w is None or hit.low_52w <= 0:
            return False
        if hit.price < self.config.min_price:
            return False
        # Live print below prior 52w low → extend the range
        if hit.price < hit.low_52w:
            hit.low_52w = hit.price
        pct = ((hit.price - hit.low_52w) / hit.low_52w) * 100.0
        return pct <= self.config.threshold_pct

    def _apply_hit_filters(
        self,
        hits: list[LowHit],
        state: ScanState,
        on_update: OnUpdate,
    ) -> list[LowHit]:
        cfg = self.config.hit_filters()
        if not cfg.any_active() or not hits:
            return hits
        kept, stats = filter_hits(hits, cfg)
        dropped = stats.input_count - stats.kept
        state.log(f"✓ {stats.summary()}")
        if dropped:
            state.log(f"· Dropped {dropped} hits via post-scan filters")
        if on_update:
            state.hits = list(kept)
            on_update(state)
        return kept

    # ── screener fallback ─────────────────────────────────────────────────

    async def _scan_screener(self, state: ScanState, *, on_update: OnUpdate) -> ScanState:
        def bump(phase: str, message: str) -> None:
            state.phase = phase
            state.message = message
            if on_update:
                on_update(state)

        bump("resolve", "Loading Public.com prebuilt 52-week low screener…")
        state.log("→ Screener mode: public.com/markets/52-week-low")
        try:
            hits, updated = await self._fetch_screener_list()
        except Exception as exc:  # noqa: BLE001
            state.error = f"Failed to load screener: {exc}"
            state.log(f"✗ {state.error}")
            state.finished = True
            bump("error", state.error)
            return state

        if self.config.limit:
            hits = hits[: self.config.limit]
        state.hits = hits
        state.total = len(hits)
        state.updated_at = updated
        state.log(f"✓ Screener list: {len(hits)} names (as of {updated or 'unknown'})")
        filters = self.config.hit_filters()
        need_enrich = self.config.enrich_hits or (
            bool(hits) and filters.needs_enrich()
        )
        if need_enrich and hits:
            if not self.config.enrich_hits and filters.needs_enrich():
                state.log(
                    "· Enabling enrich (required for active name/mcap/industry filters)"
                )
            await self._enrich_hits(hits, state, on_update)
            state.hits = hits
        hits = self._apply_hit_filters(hits, state, on_update)
        state.hits = hits
        state.finished = True
        bump("done", f"Done — {len(hits)} screener names")
        return state

    async def resolve_collection_url(self) -> str:
        try:
            resp = await self.http.get(MARKETS_PAGE)
            resp.raise_for_status()
            match = NEXT_DATA_RE.search(resp.text)
            if not match:
                return DEFAULT_COLLECTION_URL
            data = json.loads(match.group(1))
            url = (
                data.get("props", {})
                .get("pageProps", {})
                .get("assetCollection", {})
                .get("dataUrl")
            )
            if isinstance(url, str) and url.startswith("http"):
                return url
        except Exception:
            pass
        return DEFAULT_COLLECTION_URL

    async def _fetch_screener_list(self) -> tuple[list[LowHit], Optional[str]]:
        collection_url = await self.resolve_collection_url()
        resp = await self.http.get(collection_url)
        resp.raise_for_status()
        payload = resp.json()
        updated = _s(payload.get("timeOfLatestUpdate"))
        metrics = payload.get("metricsBySymbol") or {}
        hits: list[LowHit] = []
        for symbol, row in metrics.items():
            if not isinstance(row, dict):
                continue
            sym = _s(row.get("symbol") or symbol)
            if not sym:
                continue
            hits.append(
                LowHit(
                    symbol=sym.upper(),
                    price=_f(row.get("latestPrice")),
                    change_pct=_f(row.get("changeInPercentToday")),
                    change_usd=_f(row.get("changeInUsdToday")),
                    market_cap=_f(row.get("marketCap")),
                    volume=_f(row.get("singleDayTradeVolume")),
                    high_today=_f(row.get("highestTradedPriceToday")),
                    low_today=_f(row.get("lowestTradedPriceToday")),
                    previous_close=_f(row.get("priceAtLatestClose")),
                    open_price=_f(row.get("priceAtLatestOpen")),
                    quote_time=_s(row.get("timeOfQuote")),
                    included_at=_s(row.get("timeOfLatestInclusion")),
                )
            )
        hits.sort(key=lambda h: h.market_cap or 0.0, reverse=True)
        return hits, updated

    # ── enrich (stock page SSR) ───────────────────────────────────────────

    async def _enrich_hits(
        self,
        hits: list[LowHit],
        state: ScanState,
        on_update: OnUpdate,
    ) -> None:
        sem = asyncio.Semaphore(max(1, self.config.enrich_concurrency))
        done = 0
        total = len(hits)
        lock = asyncio.Lock()

        async def one(hit: LowHit) -> None:
            nonlocal done
            async with sem:
                await self.enrich_symbol(hit)
            async with lock:
                done += 1
                state.current_index = done
                state.current_symbol = hit.symbol
                if hit.enriched:
                    state.log(
                        f"✓ {hit.symbol} — {hit.display_name} | {hit.industry or 'n/a'}"
                    )
                else:
                    state.log(f"⚠ {hit.symbol} enrich failed: {hit.error}")
                state.message = f"Enriching {done}/{total}  ·  {hit.symbol}"
                if on_update:
                    on_update(state)

        # Temporarily set total for enrich progress display
        prev_total = state.total
        state.total = total
        state.phase = "enrich"
        await asyncio.gather(*(one(h) for h in hits))
        state.total = prev_total
        state.current_symbol = ""

    async def enrich_symbol(self, hit: LowHit) -> LowHit:
        url = STOCK_PAGE.format(symbol=hit.symbol.lower())
        try:
            resp = await self.http.get(url)
            resp.raise_for_status()
            match = NEXT_DATA_RE.search(resp.text)
            if not match:
                hit.error = "no __NEXT_DATA__"
                hit.enriched = False
                return hit
            data = json.loads(match.group(1))
            instrument_data = (
                data.get("props", {}).get("pageProps", {}).get("instrumentData", {})
            )
            self._apply_instrument_data(hit, instrument_data)
            hit.enriched = True
            hit.error = None
        except httpx.HTTPStatusError as exc:
            hit.error = f"HTTP {exc.response.status_code}"
            hit.enriched = False
        except Exception as exc:  # noqa: BLE001
            hit.error = str(exc)[:120]
            hit.enriched = False
        return hit

    def _apply_instrument_data(self, hit: LowHit, instrument_data: dict[str, Any]) -> None:
        instrument = instrument_data.get("instrument") or {}
        quote = instrument_data.get("quote") or {}
        company = instrument_data.get("instrumentCompanyDetails") or {}
        profile = company.get("companyProfileDetail") or {}
        fundamental = company.get("fundamental") or {}
        recommendation = company.get("recommendation") or {}

        hit.name = _s(instrument.get("displayName") or instrument.get("name")) or hit.name
        hit.company_name = _s(
            profile.get("companyName") or fundamental.get("companyName")
        )
        hit.description = _s(
            profile.get("companyDescription")
            or fundamental.get("longBusinessDescription")
        )
        hit.industry = _s(fundamental.get("industry"))
        # Prefer API-computed 52w range; fill gaps from fundamentals
        if hit.low_52w is None:
            hit.low_52w = _f(fundamental.get("lowPriceLast52Weeks"))
        if hit.high_52w is None:
            hit.high_52w = _f(fundamental.get("highPriceLast52Weeks"))
        hit.pe_ratio = _f(fundamental.get("peratio"))
        hit.beta = _f(fundamental.get("betaTwelveMonth"))
        hit.target_price = _f(recommendation.get("recommendationTargetPrice"))
        hit.short_pct_float = _s(instrument.get("shortInterestPercentageOfFloat"))

        if hit.price is None:
            hit.price = _f(quote.get("last"))
        if hit.change_pct is None:
            hit.change_pct = _f(quote.get("gainPercentage"))
        if hit.volume is None:
            hit.volume = _f(quote.get("volume"))
        if hit.high_today is None:
            hit.high_today = _f(quote.get("high"))
        if hit.low_today is None:
            hit.low_today = _f(quote.get("low"))
        if hit.previous_close is None:
            hit.previous_close = _f(quote.get("previousClose"))
        if hit.open_price is None:
            hit.open_price = _f(quote.get("open"))
        if hit.market_cap is None:
            hit.market_cap = _f(fundamental.get("marketCapitalization"))

        if recommendation:
            total = recommendation.get("totalRatings") or 0
            if total:
                strong_buy = recommendation.get("strongBuyRatings") or 0
                buy = recommendation.get("buyRatings") or 0
                hold = recommendation.get("holdRatings") or 0
                sell = recommendation.get("sellRatings") or 0
                strong_sell = recommendation.get("strongSellRatings") or 0
                hit.consensus = (
                    f"{strong_buy + buy}B/{hold}H/{sell + strong_sell}S "
                    f"(n={total}, tgt {hit.target_price or '—'})"
                )
