"""Post-scan hit filters: ETFs, SPACs, preferreds, market cap, industry."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import LowHit

# Name / description patterns (case-insensitive). Tuned against the
# 2026-08-03 / 2026-08-05 universe dumps where ~85% of raw hits were funds.
_ETF_RE = re.compile(
    r"(?:"
    r"\bETF\b|\bETN\b|\bETP\b|"
    r"exchange[\s-]?traded|"
    r"\biShares\b|\bSPDR\b|\bInvesco\b|\bVanguard\b|\bProShares\b|"
    r"\bWisdomTree\b|\biBonds\b|\bDirexion\b|\bVanEck\b|\bGlobal X\b|"
    r"\bBondBloxx\b|\bBondbloxx\b|"
    r"\bARK\b|\bSchwab\b.+\bETF\b|"
    r"\bclosed[\s-]?end\b|"
    r"(?:^|\s)(?:Ultra)?(?:Short|Long)\b.+\b(?:Fund|Trust)\b|"
    r"\bTrust\b.*\b(?:shares|currency|index)\b|"
    r"\bFund\b(?:\s|$|,|\.|:)|"  # CEFs + many bond products end in "Fund"
    r"\b(?:Trsy|Treasury)\b.+\b(?:Duration|Bond|Note)\b|"
    r"\bHigh Yield Sector Rotation\b"
    r")",
    re.I,
)

_SPAC_RE = re.compile(
    r"(?:"
    r"\bblank[\s-]?check\b|"
    r"\bSPAC\b|"
    r"acquisition\s+co(?:rp(?:oration)?|mpany)?\b|"
    r"merger\s+co(?:rp(?:oration)?|mpany)?\b|"
    r"special\s+purpose\s+acquisition\b|"
    r"formed\s+for\s+the\s+purpose\s+of\s+(?:effecting|entering)|"
    r"development\s+stage\s+company\b|"
    r"\bgigcapital\d*\b|"
    r"\bcapital\s+investment\s+corp(?:oration)?\b|"
    r"\binvestment\s+co(?:mpany)?\.?\s*"
    r"(?:ii|iii|iv|v|vi|vii|viii|ix|x|\d+)\b"
    r")",
    re.I,
)

_PREFERRED_RE = re.compile(
    r"(?:"
    r"\bpreferred\b|"
    r"\bpref(?:erence)?\s+shar"
    r")",
    re.I,
)

# Symbol-only cues (available before enrich)
_PREFERRED_SYM_RE = re.compile(r"\.PR[A-Z]?$|\.P[A-Z]$", re.I)
# Common unit share suffix on SPACs (e.g. AACIU) — weak signal alone
_UNIT_SYM_RE = re.compile(r"U$", re.I)


def parse_mktcap(value: str | float | int) -> float:
    """Parse market-cap threshold: bare number or suffix K/M/B/T (e.g. 50M, 1.5B)."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().replace(",", "").replace("$", "")
    if not text:
        raise ValueError("empty market-cap value")
    mult = 1.0
    if text[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[text[-1]]
        text = text[:-1].strip()
    return float(text) * mult


def _blob(hit: LowHit) -> str:
    parts = [
        hit.symbol or "",
        hit.name or "",
        hit.company_name or "",
        hit.description or "",
        hit.industry or "",
    ]
    return " ".join(parts)


def is_preferred(hit: LowHit) -> bool:
    sym = hit.symbol or ""
    if _PREFERRED_SYM_RE.search(sym) or ".PR" in sym.upper():
        return True
    return bool(_PREFERRED_RE.search(_blob(hit)))


def is_etf_or_fund(hit: LowHit) -> bool:
    """True for ETFs, ETNs, CEFs, and most packaged fund products."""
    return bool(_ETF_RE.search(_blob(hit)))


def is_spac(hit: LowHit) -> bool:
    blob = _blob(hit)
    if _SPAC_RE.search(blob):
        return True
    # Unit shares of blank-checks often enrich to "Acquisition Corp"
    name = f"{hit.name or ''} {hit.company_name or ''}"
    if _UNIT_SYM_RE.search(hit.symbol or "") and re.search(
        r"acquisition|merger\s+corp|blank", name, re.I
    ):
        return True
    return False


@dataclass
class FilterStats:
    input_count: int = 0
    kept: int = 0
    dropped_etf: int = 0
    dropped_spac: int = 0
    dropped_preferred: int = 0
    dropped_mktcap: int = 0
    dropped_industry: int = 0
    dropped_other: int = 0

    def summary(self) -> str:
        parts = [
            f"in={self.input_count}",
            f"kept={self.kept}",
            f"etf={self.dropped_etf}",
            f"spac={self.dropped_spac}",
            f"pref={self.dropped_preferred}",
            f"mcap={self.dropped_mktcap}",
            f"industry={self.dropped_industry}",
        ]
        if self.dropped_other:
            parts.append(f"other={self.dropped_other}")
        return "filters: " + " · ".join(parts)


@dataclass
class HitFilterConfig:
    """Which post-scan filters are active."""

    exclude_etf: bool = False
    exclude_spac: bool = False
    exclude_preferred: bool = False
    min_market_cap: Optional[float] = None
    # Substring match against hit.industry (case-insensitive). Empty = no filter.
    industries: list[str] = field(default_factory=list)
    # If True, hit must match at least one industry token; if industries empty, ignored.
    industry_include: bool = True

    def any_active(self) -> bool:
        return bool(
            self.exclude_etf
            or self.exclude_spac
            or self.exclude_preferred
            or (self.min_market_cap is not None and self.min_market_cap > 0)
            or self.industries
        )

    def needs_enrich(self) -> bool:
        """Name/desc/mcap/industry come from the stock-page enrich pass."""
        return self.any_active()


def filter_hits(
    hits: Iterable[LowHit],
    cfg: HitFilterConfig,
) -> tuple[list[LowHit], FilterStats]:
    """Apply active filters; return kept hits + drop counters."""
    stats = FilterStats()
    kept: list[LowHit] = []
    industry_needles = [s.strip().lower() for s in cfg.industries if s and s.strip()]

    for hit in hits:
        stats.input_count += 1

        if cfg.exclude_preferred and is_preferred(hit):
            stats.dropped_preferred += 1
            continue
        if cfg.exclude_etf and is_etf_or_fund(hit):
            stats.dropped_etf += 1
            continue
        if cfg.exclude_spac and is_spac(hit):
            stats.dropped_spac += 1
            continue

        if cfg.min_market_cap is not None and cfg.min_market_cap > 0:
            mcap = hit.market_cap
            # Strict: missing market cap fails when a floor is set
            if mcap is None or mcap < cfg.min_market_cap:
                stats.dropped_mktcap += 1
                continue

        if industry_needles:
            industry = (hit.industry or "").lower()
            matched = any(n in industry for n in industry_needles)
            if cfg.industry_include and not matched:
                stats.dropped_industry += 1
                continue
            if not cfg.industry_include and matched:
                stats.dropped_industry += 1
                continue

        kept.append(hit)

    stats.kept = len(kept)
    return kept, stats
