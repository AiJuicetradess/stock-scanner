"""python -m scanner"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scanner",
        description=(
            "TUI scanner: walk Public.com's full equity instrument list "
            "and find names at/near their 52-week lows"
        ),
    )
    p.add_argument(
        "--mode",
        choices=("universe", "screener"),
        default="universe",
        help="universe = full API instrument list + YEAR bars (default); "
        "screener = Public's prebuilt 52w-low page",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=2.0,
        help="Max %% above 52-week low to count as a hit (default 2.0)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only scan the first N symbols (smoke tests)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=15,
        help="Parallel YEAR-bar requests (default 15)",
    )
    p.add_argument(
        "--min-price",
        type=float,
        default=1.0,
        help="Skip names with last close below this (default 1.0)",
    )
    p.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip company-description enrich on hits "
        "(ignored when name/mcap/industry filters need enrich)",
    )
    p.add_argument(
        "--exclude-etf",
        action="store_true",
        help="Drop ETFs, ETNs, CEFs, and similar fund products (post-enrich)",
    )
    p.add_argument(
        "--exclude-spac",
        action="store_true",
        help="Drop SPACs / blank-check acquisition vehicles (post-enrich)",
    )
    p.add_argument(
        "--exclude-preferred",
        action="store_true",
        help="Drop preferred shares (symbol .PR* or name)",
    )
    p.add_argument(
        "--stocks-only",
        action="store_true",
        help="Shorthand: --exclude-etf --exclude-spac --exclude-preferred",
    )
    p.add_argument(
        "--min-mktcap",
        type=str,
        default=None,
        metavar="N",
        help="Min market cap (dollars or suffix: 50M, 1B). Drops missing mcap.",
    )
    p.add_argument(
        "--industry",
        type=str,
        default=None,
        metavar="TAGS",
        help="Comma-separated industry substrings to keep "
        "(e.g. Auto,Software,Semiconductors)",
    )
    p.add_argument(
        "--no-tui",
        action="store_true",
        help="Headless progressive log to stdout",
    )
    p.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Write hits CSV (works with --no-tui; TUI uses key e)",
    )
    p.add_argument(
        "--track",
        type=str,
        default=None,
        metavar="HITS_CSV",
        help="Track performance of a prior hits CSV vs live quotes "
        "(use with --stocks-only / --min-mktcap; skips a new scan)",
    )
    p.add_argument(
        "--out",
        type=str,
        default=None,
        help="With --track: performance CSV path "
        "(default <input>_performance.csv)",
    )
    p.add_argument(
        "--re-enrich",
        action="store_true",
        help="With --track: force stock-page enrich before filtering",
    )
    return p


def _config_from_args(args: argparse.Namespace):
    from .filters import parse_mktcap
    from .public_client import ScanConfig

    exclude_etf = bool(args.exclude_etf or args.stocks_only)
    exclude_spac = bool(args.exclude_spac or args.stocks_only)
    exclude_preferred = bool(args.exclude_preferred or args.stocks_only)
    min_mcap = parse_mktcap(args.min_mktcap) if args.min_mktcap else None
    industries = None
    if args.industry:
        industries = [p.strip() for p in args.industry.split(",") if p.strip()]

    return ScanConfig(
        mode=args.mode,
        threshold_pct=args.threshold,
        concurrency=args.concurrency,
        limit=args.limit,
        min_price=args.min_price,
        enrich_hits=not args.no_enrich,
        exclude_etf=exclude_etf,
        exclude_spac=exclude_spac,
        exclude_preferred=exclude_preferred,
        min_market_cap=min_mcap,
        industries=industries,
    )


def _write_csv(path: Path, hits) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "symbol",
        "name",
        "price",
        "change_pct",
        "market_cap",
        "volume",
        "low_52w",
        "high_52w",
        "pct_from_52w_low",
        "industry",
        "description",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for hit in hits:
            w.writerow(
                {
                    "symbol": hit.symbol,
                    "name": hit.name or hit.company_name,
                    "price": hit.price,
                    "change_pct": hit.change_pct,
                    "market_cap": hit.market_cap,
                    "volume": hit.volume,
                    "low_52w": hit.low_52w,
                    "high_52w": hit.high_52w,
                    "pct_from_52w_low": hit.pct_from_52w_low,
                    "industry": hit.industry,
                    "description": hit.description,
                }
            )


async def run_headless(args: argparse.Namespace) -> int:
    from .models import ScanState, fmt_mcap, fmt_money, fmt_pct
    from .public_client import PublicLowScanner

    config = _config_from_args(args)
    state = ScanState(mode=config.mode, threshold_pct=config.threshold_pct)
    last_len = 0

    def on_update(s: ScanState) -> None:
        nonlocal last_len
        for line in s.log_lines[last_len:]:
            print(line, flush=True)
        last_len = len(s.log_lines)

    async with PublicLowScanner(config) as client:
        await client.scan(state, on_update=on_update)

    if state.error and not state.hits and state.scanned == 0:
        print(f"ERROR: {state.error}", file=sys.stderr)
        return 1

    print()
    print(
        f"{'TICKER':<8} {'PRICE':>10} {'FROM LOW':>10} {'52W LOW':>10} "
        f"{'52W HIGH':>10}  NAME"
    )
    print("-" * 80)
    for hit in state.hits:
        print(
            f"{hit.symbol:<8} {fmt_money(hit.price):>10} "
            f"{fmt_pct(hit.pct_from_52w_low):>10} "
            f"{fmt_money(hit.low_52w):>10} {fmt_money(hit.high_52w):>10}  "
            f"{hit.display_name}"
        )
        if hit.description:
            print(f"         {hit.short_description}")

    print()
    filter_bits = []
    if config.exclude_etf:
        filter_bits.append("no-etf")
    if config.exclude_spac:
        filter_bits.append("no-spac")
    if config.exclude_preferred:
        filter_bits.append("no-pref")
    if config.min_market_cap:
        filter_bits.append(f"mcap≥{config.min_market_cap:,.0f}")
    if config.industries:
        filter_bits.append("industry=" + "|".join(config.industries))
    filt = f"  ·  Filters: {', '.join(filter_bits)}" if filter_bits else ""
    print(
        f"Hits: {len(state.hits)}  ·  Scanned: {state.scanned or state.total:,}  "
        f"·  Threshold: ≤{config.threshold_pct:g}% from 52w low{filt}"
    )

    if args.csv:
        path = Path(args.csv)
        _write_csv(path, state.hits)
        print(f"CSV written: {path}")

    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.track:
        from .performance import hit_filters_from_args, run_track

        raise SystemExit(
            asyncio.run(
                run_track(
                    Path(args.track),
                    hit_filters=hit_filters_from_args(args),
                    out_path=Path(args.out) if args.out else None,
                    re_enrich=bool(args.re_enrich),
                )
            )
        )
    if args.no_tui:
        raise SystemExit(asyncio.run(run_headless(args)))
    from .tui import run_app

    run_app(_config_from_args(args))


if __name__ == "__main__":
    main()
