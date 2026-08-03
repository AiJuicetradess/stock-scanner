# Public.com 52-Week Low Scanner

TUI that walks **Public.com’s full equity instrument list** (~12k BUY_AND_SELL names), pulls a year of bars per ticker via the **Public Trading API**, and surfaces names sitting at or near their **52-week low**.

```powershell
cd stock-scanner
python -m pip install -r requirements.txt
python -m scanner
```

## Auth

Credentials live in `.env` (already gitignored if you add a `.gitignore`):

```
PUBLIC_API_SECRET_KEY=your_secret
PUBLIC_COM_ACCOUNT_ID=your_account_number
```

Create the API key in Public.com account settings. The scanner never prints secrets.

## How it works

1. **Universe** — `get_all_instruments(EQUITY, BUY_AND_SELL)` (~12k symbols)
2. **Scan** — concurrent `get_bars(symbol, YEAR)` → min low / max high / last close
3. **Hit rule** — last close within `--threshold` % of the 52-week low (default **2%**), or today’s bar low prints a new 52w low
4. **Quotes** — batch refresh live quotes on hits
5. **Enrich** — optional company description / industry from `public.com/stocks/{ticker}`
6. **TUI** — live progress, rate, ETA, growing hits table, detail pane, CSV export

Also supports **`--mode screener`** to use Public’s prebuilt 52-week-low page (no bar math).

## Commands

```powershell
# Full interactive TUI (default: universe, 2% threshold)
python -m scanner

# Tighter definition of "at the low"
python -m scanner --threshold 0.5

# Faster smoke test
python -m scanner --no-tui --limit 200 --threshold 3 --csv output\smoke.csv

# Prebuilt Public screener only (no full universe)
python -m scanner --mode screener

# Headless full run
python -m scanner --no-tui --csv output\universe_lows.csv
```

### TUI keys

| Key | Action     |
|-----|------------|
| `q` | Quit       |
| `r` | Rescan     |
| `e` | Export CSV |
| ↑/↓ | Select hit |

## Performance

On this account, YEAR bars run ~60–70 symbols/sec at concurrency 15–25 → full ~12k universe in roughly **3–4 minutes**, then a short enrich pass on hits only.

```powershell
python -m scanner --concurrency 20
```

## Filters

| Flag | Default | Meaning |
|------|---------|---------|
| `--threshold` | 2.0 | Max % above 52w low |
| `--min-price` | 1.0 | Skip sub-$1 last close |
| `--concurrency` | 15 | Parallel bar requests |
| `--limit` | none | Cap universe size |
| `--no-enrich` | off | Skip description fetch |

## Layout

```
stock-scanner/
  .env                 # API secret + account (local)
  requirements.txt
  scanner/
    __main__.py        # CLI
    config.py          # credential load
    public_client.py   # universe + screener scan
    models.py
    tui.py
  output/              # CSV exports
```
