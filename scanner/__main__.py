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
        help="Skip company-description enrich on hits",
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
    return p


def _config_from_args(args: argparse.Namespace):
    from .public_client import ScanConfig

    return ScanConfig(
        mode=args.mode,
        threshold_pct=args.threshold,
        concurrency=args.concurrency,
        limit=args.limit,
        min_price=args.min_price,
        enrich_hits=not args.no_enrich,
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
    print(
        f"Hits: {len(state.hits)}  ·  Scanned: {state.scanned or state.total:,}  "
        f"·  Threshold: ≤{config.threshold_pct:g}% from 52w low"
    )

    if args.csv:
        path = Path(args.csv)
        _write_csv(path, state.hits)
        print(f"CSV written: {path}")

    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.no_tui:
        raise SystemExit(asyncio.run(run_headless(args)))
    from .tui import run_app

    run_app(_config_from_args(args))


if __name__ == "__main__":
    main()
