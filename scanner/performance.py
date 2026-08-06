"""Track how a prior 52w-low hit CSV performed vs live quotes.

Example (stocks-only, so bond ETFs don't drown the signal):

  python -m scanner --track output/52w_lows_universe_20260803_061151.csv \\
      --stocks-only --out output/aug3_stocks_performance.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .filters import HitFilterConfig, filter_hits, parse_mktcap
from .models import LowHit


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


def load_hits_csv(path: Path) -> list[LowHit]:
    """Load a scanner export (or performance re-export) into LowHit rows."""
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        hits: list[LowHit] = []
        for row in reader:
            sym = _s(row.get("symbol"))
            if not sym:
                continue
            price = _f(row.get("price") or row.get("scan_price"))
            hits.append(
                LowHit(
                    symbol=sym.upper(),
                    name=_s(row.get("name")),
                    company_name=_s(row.get("company_name")),
                    price=price,
                    change_pct=_f(row.get("change_pct")),
                    market_cap=_f(row.get("market_cap")),
                    volume=_f(row.get("volume")),
                    low_52w=_f(row.get("low_52w") or row.get("low_52w_scan")),
                    high_52w=_f(row.get("high_52w")),
                    industry=_s(row.get("industry")),
                    description=_s(row.get("description")),
                    pe_ratio=_f(row.get("pe_ratio")),
                    beta=_f(row.get("beta")),
                    enriched=bool(
                        row.get("description")
                        or row.get("industry")
                        or row.get("name")
                    ),
                )
            )
        return hits


@dataclass
class PerfRow:
    symbol: str
    name: str
    scan_price: float
    cur_price: float
    ret_pct: float
    low_52w_scan: Optional[float]
    pct_from_low_now: Optional[float]
    broke_lower: bool
    price_src: str
    industry: str
    market_cap: Optional[float]


@dataclass
class PerfSummary:
    n: int
    missing: int
    mean: float
    median: float
    p10: float
    p25: float
    p75: float
    p90: float
    up_any: int
    down_any: int
    bounce_0_5: int
    flat: int
    sink_0_5: int
    bounce_2: int
    bounce_5: int
    sink_2: int
    sink_5: int
    still_le_2: int
    left_2: int
    still_le_5: int
    broke_lower: int
    buckets: dict[str, int]


def _pctile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(rows: list[PerfRow]) -> PerfSummary:
    rets = [r.ret_pct for r in rows]
    n = len(rows)
    buckets = {
        "<-10%": 0,
        "-10..-5%": 0,
        "-5..-2%": 0,
        "-2..-0.5%": 0,
        "flat ±0.5%": 0,
        "+0.5..+2%": 0,
        "+2..+5%": 0,
        "+5..+10%": 0,
        ">+10%": 0,
    }
    for x in rets:
        if x < -10:
            buckets["<-10%"] += 1
        elif x < -5:
            buckets["-10..-5%"] += 1
        elif x < -2:
            buckets["-5..-2%"] += 1
        elif x < -0.5:
            buckets["-2..-0.5%"] += 1
        elif x <= 0.5:
            buckets["flat ±0.5%"] += 1
        elif x <= 2:
            buckets["+0.5..+2%"] += 1
        elif x <= 5:
            buckets["+2..+5%"] += 1
        elif x <= 10:
            buckets["+5..+10%"] += 1
        else:
            buckets[">+10%"] += 1

    still_le_2 = sum(
        1 for r in rows if r.pct_from_low_now is not None and r.pct_from_low_now <= 2.0
    )
    left_2 = sum(
        1 for r in rows if r.pct_from_low_now is not None and r.pct_from_low_now > 2.0
    )
    still_le_5 = sum(
        1 for r in rows if r.pct_from_low_now is not None and r.pct_from_low_now <= 5.0
    )

    return PerfSummary(
        n=n,
        missing=0,
        mean=statistics.mean(rets) if rets else 0.0,
        median=statistics.median(rets) if rets else 0.0,
        p10=_pctile(rets, 10),
        p25=_pctile(rets, 25),
        p75=_pctile(rets, 75),
        p90=_pctile(rets, 90),
        up_any=sum(1 for x in rets if x > 0),
        down_any=sum(1 for x in rets if x < 0),
        bounce_0_5=sum(1 for x in rets if x > 0.5),
        flat=sum(1 for x in rets if abs(x) <= 0.5),
        sink_0_5=sum(1 for x in rets if x < -0.5),
        bounce_2=sum(1 for x in rets if x >= 2.0),
        bounce_5=sum(1 for x in rets if x >= 5.0),
        sink_2=sum(1 for x in rets if x <= -2.0),
        sink_5=sum(1 for x in rets if x <= -5.0),
        still_le_2=still_le_2,
        left_2=left_2,
        still_le_5=still_le_5,
        broke_lower=sum(1 for r in rows if r.broke_lower),
        buckets=buckets,
    )


async def fetch_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Batch Public quotes. Prefer last > 0, else previous_close."""
    from public_api_sdk import (
        ApiKeyAuthConfig,
        AsyncPublicApiClient,
        AsyncPublicApiClientConfiguration,
        InstrumentType,
        OrderInstrument,
    )

    from .config import PublicCredentials

    creds = PublicCredentials.from_env()
    out: dict[str, dict[str, Any]] = {}
    async with AsyncPublicApiClient(
        auth_config=ApiKeyAuthConfig(
            api_secret_key=creds.api_secret_key,
            validity_minutes=60,
        ),
        config=AsyncPublicApiClientConfiguration(
            default_account_number=creds.account_id
        ),
    ) as api:
        chunk = 40
        for i in range(0, len(symbols), chunk):
            batch = symbols[i : i + chunk]
            instruments = [
                OrderInstrument(symbol=s, type=InstrumentType.EQUITY) for s in batch
            ]
            try:
                quotes = await api.get_quotes(instruments)
            except Exception as exc:  # noqa: BLE001
                print(f"  quote batch {i} failed: {exc}", flush=True)
                continue
            for q in quotes or []:
                inst = getattr(q, "instrument", None)
                sym = getattr(inst, "symbol", None)
                if not sym:
                    continue
                last = _f(getattr(q, "last", None))
                prev = _f(getattr(q, "previous_close", None))
                if last is not None and last > 0:
                    px, src = last, "last"
                elif prev is not None and prev > 0:
                    px, src = prev, "prev"
                else:
                    px, src = None, "none"
                out[str(sym).upper()] = {"px": px, "src": src, "last": last, "prev": prev}
            done = min(i + chunk, len(symbols))
            if i == 0 or done == len(symbols) or (i // chunk) % 4 == 0:
                print(f"  quotes {done}/{len(symbols)}", flush=True)
    return out


def build_perf_rows(
    hits: list[LowHit],
    quotes: dict[str, dict[str, Any]],
) -> tuple[list[PerfRow], int]:
    rows: list[PerfRow] = []
    missing = 0
    for hit in hits:
        scan = hit.price
        if scan is None or scan <= 0:
            missing += 1
            continue
        q = quotes.get(hit.symbol.upper(), {})
        cur = q.get("px")
        src = q.get("src") or "none"
        if cur is None or cur <= 0:
            missing += 1
            continue
        low = hit.low_52w
        from_low = ((cur - low) / low * 100.0) if low and low > 0 else None
        broke = bool(low is not None and low > 0 and cur < low * 0.999)
        rows.append(
            PerfRow(
                symbol=hit.symbol,
                name=hit.display_name,
                scan_price=scan,
                cur_price=float(cur),
                ret_pct=(float(cur) - scan) / scan * 100.0,
                low_52w_scan=low,
                pct_from_low_now=from_low,
                broke_lower=broke,
                price_src=str(src),
                industry=hit.industry or "",
                market_cap=hit.market_cap,
            )
        )
    return rows, missing


def write_perf_csv(path: Path, rows: list[PerfRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "symbol",
        "name",
        "scan_price",
        "cur_price",
        "ret_pct",
        "low_52w_scan",
        "pct_from_low_now",
        "broke_lower",
        "price_src",
        "industry",
        "market_cap",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in sorted(rows, key=lambda x: x.ret_pct, reverse=True):
            w.writerow(
                {
                    "symbol": r.symbol,
                    "name": r.name,
                    "scan_price": round(r.scan_price, 4),
                    "cur_price": round(r.cur_price, 4),
                    "ret_pct": round(r.ret_pct, 3),
                    "low_52w_scan": r.low_52w_scan,
                    "pct_from_low_now": (
                        None
                        if r.pct_from_low_now is None
                        else round(r.pct_from_low_now, 3)
                    ),
                    "broke_lower": r.broke_lower,
                    "price_src": r.price_src,
                    "industry": r.industry,
                    "market_cap": r.market_cap,
                }
            )


def print_summary(
    title: str,
    summary: PerfSummary,
    missing: int,
    input_n: int,
    filter_note: str,
    top: list[PerfRow],
    bottom: list[PerfRow],
) -> None:
    n = summary.n
    print()
    print(f"=== {title} ===")
    if filter_note:
        print(f"Filters: {filter_note}")
    print(f"Input rows: {input_n}  ·  priced: {n}  ·  missing quote/price: {missing}")
    if n == 0:
        print("No priced rows to summarize.")
        return
    print(f"Mean {summary.mean:+.2f}%   median {summary.median:+.2f}%")
    print(
        f"p10 {summary.p10:+.2f}% | p25 {summary.p25:+.2f}% | "
        f"p75 {summary.p75:+.2f}% | p90 {summary.p90:+.2f}%"
    )
    print(
        f"any up {summary.up_any} ({100*summary.up_any/n:.1f}%)  "
        f"any down {summary.down_any} ({100*summary.down_any/n:.1f}%)"
    )
    print(
        f"bounce >+0.5%: {summary.bounce_0_5} ({100*summary.bounce_0_5/n:.1f}%)  "
        f"flat ±0.5%: {summary.flat} ({100*summary.flat/n:.1f}%)  "
        f"sink <-0.5%: {summary.sink_0_5} ({100*summary.sink_0_5/n:.1f}%)"
    )
    print(
        f"≥+2%: {summary.bounce_2} ({100*summary.bounce_2/n:.1f}%)  "
        f"≥+5%: {summary.bounce_5} ({100*summary.bounce_5/n:.1f}%)  "
        f"≤-2%: {summary.sink_2} ({100*summary.sink_2/n:.1f}%)  "
        f"≤-5%: {summary.sink_5} ({100*summary.sink_5/n:.1f}%)"
    )
    print(
        f"still ≤2% of scan low: {summary.still_le_2} ({100*summary.still_le_2/n:.1f}%)  "
        f"left zone: {summary.left_2} ({100*summary.left_2/n:.1f}%)  "
        f"still ≤5%: {summary.still_le_5} ({100*summary.still_le_5/n:.1f}%)"
    )
    print(f"broke under scan 52w low: {summary.broke_lower} ({100*summary.broke_lower/n:.1f}%)")
    print("return buckets:")
    for key in [
        "<-10%",
        "-10..-5%",
        "-5..-2%",
        "-2..-0.5%",
        "flat ±0.5%",
        "+0.5..+2%",
        "+2..+5%",
        "+5..+10%",
        ">+10%",
    ]:
        c = summary.buckets[key]
        print(f"  {key:14} {c:4}  {100*c/n:5.1f}%")
    print("TOP bounces:")
    for r in top:
        fl = f"{r.pct_from_low_now:+.1f}%" if r.pct_from_low_now is not None else "n/a"
        print(
            f"  {r.symbol:10} {r.ret_pct:+7.2f}%  "
            f"{r.scan_price:.2f}->{r.cur_price:.2f}  lowΔ {fl}  {r.name[:42]}"
        )
    print("WORST:")
    for r in bottom:
        fl = f"{r.pct_from_low_now:+.1f}%" if r.pct_from_low_now is not None else "n/a"
        print(
            f"  {r.symbol:10} {r.ret_pct:+7.2f}%  "
            f"{r.scan_price:.2f}->{r.cur_price:.2f}  lowΔ {fl}  {r.name[:42]}"
        )


def filter_note(cfg: HitFilterConfig) -> str:
    parts: list[str] = []
    if cfg.exclude_etf:
        parts.append("no-etf")
    if cfg.exclude_spac:
        parts.append("no-spac")
    if cfg.exclude_preferred:
        parts.append("no-pref")
    if cfg.min_market_cap:
        parts.append(f"mcap≥{cfg.min_market_cap:,.0f}")
    if cfg.industries:
        parts.append("industry=" + "|".join(cfg.industries))
    return ", ".join(parts) if parts else "(none — full basket)"


async def run_track(
    csv_path: Path,
    *,
    hit_filters: HitFilterConfig,
    out_path: Optional[Path],
    re_enrich: bool = False,
) -> int:
    if not csv_path.is_file():
        print(f"ERROR: file not found: {csv_path}", file=sys.stderr)
        return 1

    hits = load_hits_csv(csv_path)
    print(f"Loaded {len(hits)} rows from {csv_path}")

    if hit_filters.any_active():
        # Optional re-enrich when name/desc thin and filters need text
        need_text = hit_filters.exclude_etf or hit_filters.exclude_spac
        thin = sum(1 for h in hits if not (h.name and h.name != h.symbol) and not h.description)
        if re_enrich or (need_text and thin > len(hits) * 0.5):
            print(f"Re-enriching {len(hits)} hits for filter text…", flush=True)
            from .public_client import PublicLowScanner, ScanConfig
            from .models import ScanState

            async with PublicLowScanner(ScanConfig(enrich_hits=True)) as client:
                state = ScanState()
                await client._enrich_hits(hits, state, on_update=None)

        kept, stats = filter_hits(hits, hit_filters)
        print(f"✓ {stats.summary()}")
        hits = kept
    else:
        print("No filters active — tracking full basket")

    if not hits:
        print("Nothing left after filters.")
        return 0

    symbols = [h.symbol for h in hits if h.price and h.price > 0]
    print(f"Fetching live quotes for {len(symbols)} symbols…", flush=True)
    quotes = await fetch_quotes(symbols)
    rows, missing = build_perf_rows(hits, quotes)
    summary = summarize(rows)
    summary.missing = missing

    top = sorted(rows, key=lambda r: r.ret_pct, reverse=True)[:12]
    bottom = sorted(rows, key=lambda r: r.ret_pct)[:12]
    print_summary(
        title=f"Performance track — {csv_path.name}",
        summary=summary,
        missing=missing,
        input_n=len(hits),
        filter_note=filter_note(hit_filters),
        top=top,
        bottom=bottom,
    )

    if out_path is None:
        out_path = csv_path.with_name(csv_path.stem + "_performance.csv")
    write_perf_csv(out_path, rows)
    print(f"\nCSV written: {out_path}")
    return 0


def build_track_filter_args(p: argparse.ArgumentParser) -> None:
    """Shared filter flags for --track mode (mirrors scan CLI)."""
    p.add_argument(
        "--exclude-etf",
        action="store_true",
        help="Drop ETFs / ETNs / CEFs",
    )
    p.add_argument(
        "--exclude-spac",
        action="store_true",
        help="Drop SPACs / blank-checks",
    )
    p.add_argument(
        "--exclude-preferred",
        action="store_true",
        help="Drop preferred shares",
    )
    p.add_argument(
        "--stocks-only",
        action="store_true",
        help="Shorthand: exclude ETF + SPAC + preferred",
    )
    p.add_argument(
        "--min-mktcap",
        type=str,
        default=None,
        metavar="N",
        help="Min market cap (50M, 1B, …)",
    )
    p.add_argument(
        "--industry",
        type=str,
        default=None,
        metavar="TAGS",
        help="Comma-separated industry substrings to keep",
    )
    p.add_argument(
        "--re-enrich",
        action="store_true",
        help="Force stock-page enrich before filtering (slower)",
    )
    p.add_argument(
        "--out",
        type=str,
        default=None,
        help="Performance CSV path (default: <input>_performance.csv)",
    )


def hit_filters_from_args(args: argparse.Namespace) -> HitFilterConfig:
    stocks = bool(getattr(args, "stocks_only", False))
    industries = None
    if getattr(args, "industry", None):
        industries = [p.strip() for p in args.industry.split(",") if p.strip()]
    min_mcap = parse_mktcap(args.min_mktcap) if getattr(args, "min_mktcap", None) else None
    return HitFilterConfig(
        exclude_etf=bool(getattr(args, "exclude_etf", False) or stocks),
        exclude_spac=bool(getattr(args, "exclude_spac", False) or stocks),
        exclude_preferred=bool(getattr(args, "exclude_preferred", False) or stocks),
        min_market_cap=min_mcap,
        industries=list(industries or []),
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="scanner.performance",
        description="Track performance of a prior 52w-low hits CSV vs live quotes",
    )
    p.add_argument(
        "csv",
        type=str,
        help="Path to prior scan CSV (e.g. output/52w_lows_universe_….csv)",
    )
    build_track_filter_args(p)
    args = p.parse_args(argv)
    raise SystemExit(
        asyncio.run(
            run_track(
                Path(args.csv),
                hit_filters=hit_filters_from_args(args),
                out_path=Path(args.out) if args.out else None,
                re_enrich=bool(args.re_enrich),
            )
        )
    )


if __name__ == "__main__":
    main()
