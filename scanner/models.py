from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def fmt_money(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"${float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def fmt_pct(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_mcap(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    abs_v = abs(v)
    if abs_v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if abs_v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs_v >= 1e6:
        return f"${v / 1e6:.2f}M"
    return f"${v:,.0f}"


def fmt_volume(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v >= 1e9:
        return f"{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:,.0f}"


@dataclass
class LowHit:
    """One name near its 52-week low (API universe scan or screener)."""

    symbol: str
    price: Optional[float] = None
    change_pct: Optional[float] = None
    change_usd: Optional[float] = None
    market_cap: Optional[float] = None
    volume: Optional[float] = None
    high_today: Optional[float] = None
    low_today: Optional[float] = None
    previous_close: Optional[float] = None
    open_price: Optional[float] = None
    quote_time: Optional[str] = None
    included_at: Optional[str] = None

    # Enriched from stock page
    name: Optional[str] = None
    company_name: Optional[str] = None
    description: Optional[str] = None
    industry: Optional[str] = None
    low_52w: Optional[float] = None
    high_52w: Optional[float] = None
    pe_ratio: Optional[float] = None
    beta: Optional[float] = None
    target_price: Optional[float] = None
    consensus: Optional[str] = None
    short_pct_float: Optional[str] = None
    exchange_hint: Optional[str] = None
    error: Optional[str] = None
    enriched: bool = False

    @property
    def display_name(self) -> str:
        return self.name or self.company_name or self.symbol

    @property
    def pct_from_52w_low(self) -> Optional[float]:
        if self.price is None or self.low_52w is None or self.low_52w <= 0:
            return None
        return ((self.price - self.low_52w) / self.low_52w) * 100.0

    @property
    def short_description(self) -> str:
        if not self.description:
            return "—"
        text = " ".join(self.description.split())
        return text if len(text) <= 140 else text[:137] + "..."

    def as_table_row(self) -> tuple[str, ...]:
        return (
            self.symbol,
            (self.display_name or "")[:28],
            fmt_money(self.price),
            fmt_pct(self.change_pct),
            fmt_mcap(self.market_cap),
            fmt_money(self.low_52w),
            fmt_money(self.high_52w),
            fmt_pct(self.pct_from_52w_low),
            (self.industry or "—")[:18],
            self.short_description[:60],
        )


@dataclass
class ScanState:
    """Live scan progress for the TUI."""

    phase: str = "idle"
    message: str = "Ready"
    total: int = 0
    current_index: int = 0
    current_symbol: str = ""
    hits: list[LowHit] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    updated_at: Optional[str] = None
    error: Optional[str] = None
    finished: bool = False
    universe_size: int = 0
    scanned: int = 0
    error_count: int = 0
    scan_rate: float = 0.0
    mode: str = "universe"
    threshold_pct: float = 2.0

    def log(self, line: str) -> None:
        self.log_lines.append(line)
        # keep the panel readable
        if len(self.log_lines) > 400:
            self.log_lines = self.log_lines[-300:]

    @property
    def progress_fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.current_index / self.total)
