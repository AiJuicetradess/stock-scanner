"""Textual TUI for the Public.com full-universe 52-week low scanner."""

from __future__ import annotations

from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    ProgressBar,
    RichLog,
    Static,
)

from .models import LowHit, ScanState, fmt_mcap, fmt_money, fmt_pct, fmt_volume
from .public_client import PublicLowScanner, ScanConfig

COLUMNS = [
    ("sym", "Ticker", 8),
    ("name", "Name", 22),
    ("price", "Price", 10),
    ("chg", "Chg%", 9),
    ("mcap", "Mkt Cap", 10),
    ("low52", "52w Low", 10),
    ("high52", "52w High", 10),
    ("from_low", "From Low", 10),
    ("ind", "Industry", 16),
    ("desc", "Description", 48),
]


class StatusBanner(Static):
    DEFAULT_CSS = """
    StatusBanner {
        height: 3;
        padding: 0 1;
        background: $surface;
        color: $text;
        border: solid $primary;
    }
    """


class DetailPane(Static):
    DEFAULT_CSS = """
    DetailPane {
        height: 10;
        padding: 0 1;
        border: solid $accent;
        background: $panel;
    }
    """

    def show_hit(self, hit: Optional[LowHit]) -> None:
        if hit is None:
            self.update("[dim]Select a hit for full company details[/]")
            return
        desc = hit.description or "No description available yet."
        lines = [
            f"[b cyan]{hit.symbol}[/]  [b]{hit.display_name}[/]",
            (
                f"Price {fmt_money(hit.price)}  ({fmt_pct(hit.change_pct)})   "
                f"Mkt Cap {fmt_mcap(hit.market_cap)}   Vol {fmt_volume(hit.volume)}"
            ),
            (
                f"52w range {fmt_money(hit.low_52w)} – {fmt_money(hit.high_52w)}   "
                f"from low {fmt_pct(hit.pct_from_52w_low)}   "
                f"Industry: {hit.industry or '—'}"
            ),
            (
                f"P/E {hit.pe_ratio if hit.pe_ratio is not None else '—'}   "
                f"Beta {hit.beta if hit.beta is not None else '—'}   "
                f"Short % float {hit.short_pct_float or '—'}   "
                f"Analysts: {hit.consensus or '—'}"
            ),
            "",
            f"[dim]{desc}[/]",
        ]
        if hit.error:
            lines.append(f"[red]Enrich error: {hit.error}[/]")
        self.update("\n".join(lines))


class LowScannerApp(App[None]):
    """Walk the full Public equity universe and surface true 52-week lows."""

    TITLE = "Public.com 52-Week Low Scanner"
    SUB_TITLE = "full instrument universe via Public API"
    CSS = """
    Screen { layout: vertical; }
    #banner { dock: top; height: 3; }
    #progress_row { height: 1; padding: 0 1; }
    #main { height: 1fr; }
    #table { height: 1fr; border: solid $primary; }
    #side { width: 44; border: solid $secondary; }
    #log { height: 1fr; }
    #detail { height: 10; }
    .muted { color: $text-muted; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("e", "export", "Export CSV"),
    ]

    status_text: reactive[str] = reactive("Starting…")

    def __init__(self, config: Optional[ScanConfig] = None) -> None:
        super().__init__()
        self.config = config or ScanConfig()
        self.state = ScanState(
            mode=self.config.mode,
            threshold_pct=self.config.threshold_pct,
        )
        self._row_keys: list[str] = []
        self._scan_task_running = False
        self._log_count = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBanner(id="banner")
        with Horizontal(id="progress_row"):
            yield Label("Progress ", classes="muted")
            yield ProgressBar(total=100, show_eta=False, id="progress")
        with Horizontal(id="main"):
            yield DataTable(id="table", zebra_stripes=True, cursor_type="row")
            with Vertical(id="side"):
                yield Label("[b]Scan activity[/]")
                yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield DetailPane(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        for key, label, width in COLUMNS:
            table.add_column(label, key=key, width=width)
        self.query_one("#detail", DetailPane).show_hit(None)
        self._set_banner("Booting scanner…")
        self.run_scan()

    def _set_banner(self, text: str) -> None:
        self.status_text = text
        phase = (self.state.phase or "idle").upper()
        cur = self.state.current_symbol or "—"
        hits = len(self.state.hits)
        total = self.state.total or 0
        done = self.state.current_index
        rate = self.state.scan_rate
        thr = self.state.threshold_pct
        mode = self.state.mode
        banner = (
            f"[b]{phase}[/]  {text}\n"
            f"Mode: [cyan]{mode}[/]  ≤{thr:g}% from 52w low  ·  "
            f"Current: [cyan]{cur}[/]  {done:,}/{total:,}  ·  "
            f"Hits: [green]{hits}[/]  ·  {rate:.0f}/s  ·  err {self.state.error_count}"
        )
        self.query_one("#banner", StatusBanner).update(banner)

    def _sync_progress(self) -> None:
        bar = self.query_one("#progress", ProgressBar)
        total = max(self.state.total, 1)
        bar.update(total=total, progress=self.state.current_index)

    def _append_logs(self) -> None:
        log = self.query_one("#log", RichLog)
        new_lines = self.state.log_lines[self._log_count :]
        for line in new_lines:
            log.write(line)
        self._log_count = len(self.state.log_lines)

    def _rebuild_table(self) -> None:
        table = self.query_one("#table", DataTable)
        try:
            cursor_row = table.cursor_row
        except Exception:
            cursor_row = 0
        table.clear()
        self._row_keys.clear()
        for hit in self.state.hits:
            table.add_row(*hit.as_table_row(), key=hit.symbol)
            self._row_keys.append(hit.symbol)
        if self._row_keys:
            try:
                table.move_cursor(row=min(cursor_row, len(self._row_keys) - 1))
            except Exception:
                pass

    def _apply_state(self, state: ScanState) -> None:
        self.state = state
        self._set_banner(state.message)
        self._sync_progress()
        self._append_logs()
        self._rebuild_table()

    @work(exclusive=True, group="scan")
    async def run_scan(self) -> None:
        if self._scan_task_running:
            return
        self._scan_task_running = True
        self.state = ScanState(
            mode=self.config.mode,
            threshold_pct=self.config.threshold_pct,
        )
        self._log_count = 0
        self._row_keys.clear()
        try:
            self.query_one("#table", DataTable).clear()
            self.query_one("#log", RichLog).clear()
        except Exception:
            pass

        try:
            async with PublicLowScanner(self.config) as scanner:

                def ui_update(s: ScanState) -> None:
                    self._apply_state(s)

                await scanner.scan(self.state, on_update=ui_update)
        except Exception as exc:  # noqa: BLE001
            self.state.error = str(exc)
            self.state.log(f"✗ Fatal: {exc}")
            self.state.finished = True
            self._apply_state(self.state)
        finally:
            self._scan_task_running = False

    def action_rescan(self) -> None:
        if self._scan_task_running:
            self.notify("Scan already running", severity="warning")
            return
        self.notify("Restarting full universe scan…")
        self.run_scan()

    def action_export(self) -> None:
        if not self.state.hits:
            self.notify("No hits to export yet", severity="warning")
            return
        path = self._export_csv()
        self.notify(f"Wrote {path}")

    def _export_csv(self) -> str:
        import csv
        from datetime import datetime, timezone
        from pathlib import Path

        out_dir = Path(__file__).resolve().parent.parent / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"52w_lows_universe_{stamp}.csv"
        fields = [
            "symbol",
            "name",
            "company_name",
            "price",
            "change_pct",
            "market_cap",
            "volume",
            "low_52w",
            "high_52w",
            "pct_from_52w_low",
            "industry",
            "pe_ratio",
            "beta",
            "target_price",
            "consensus",
            "short_pct_float",
            "description",
        ]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for hit in self.state.hits:
                writer.writerow(
                    {
                        "symbol": hit.symbol,
                        "name": hit.name,
                        "company_name": hit.company_name,
                        "price": hit.price,
                        "change_pct": hit.change_pct,
                        "market_cap": hit.market_cap,
                        "volume": hit.volume,
                        "low_52w": hit.low_52w,
                        "high_52w": hit.high_52w,
                        "pct_from_52w_low": hit.pct_from_52w_low,
                        "industry": hit.industry,
                        "pe_ratio": hit.pe_ratio,
                        "beta": hit.beta,
                        "target_price": hit.target_price,
                        "consensus": hit.consensus,
                        "short_pct_float": hit.short_pct_float,
                        "description": hit.description,
                    }
                )
        return str(path)

    @on(DataTable.RowHighlighted)
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_selected(event.row_key)

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_selected(event.row_key)

    def _show_selected(self, row_key: object) -> None:
        if row_key is None:
            return
        key = str(getattr(row_key, "value", row_key))
        hit = next((h for h in self.state.hits if h.symbol == key), None)
        self.query_one("#detail", DetailPane).show_hit(hit)


def run_app(config: Optional[ScanConfig] = None) -> None:
    LowScannerApp(config=config).run()
