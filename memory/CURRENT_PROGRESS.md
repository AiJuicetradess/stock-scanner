# CURRENT_PROGRESS — stock-scanner

**Last updated:** 2026-08-06  
**Status:** v0.2 — filters + performance track shipped; full-universe scan + stocks-only signal path

## Pickup first

1. Re-scan stocks-only when needed:  
   `python -m scanner --no-tui --stocks-only --min-mktcap 50M --csv output/universe_lows_filtered.csv`
2. Track a prior dump:  
   `python -m scanner --track output/52w_lows_universe_20260803_061151.csv --stocks-only --out output/aug3_stocks_performance.csv`
3. Optional: tighten residual fund leaks (municipal/bond names, InvestmentTrusts industry)
4. Optional: deeper briefs on XPEV / GPI / BV

## Session outcomes (2026-08-05)

- Full scan ~12k → ~666 raw hits; stocks-only → handful of real names
- Aug 3 953-name list: full basket flat (~0% median); stocks-only mean ~+2%, ~1/3 ≥+2% bounce
- Notable at lows (stocks-only): **XPEV, GPI, HNLGY, FURCF, BV, SUJA, EVI, ACR, BRID, FKWL**

## Canonical docs

- `README.md` — install, auth, flags, filters, `--track`
- `scanner/filters.py` — ETF/SPAC/pref/mcap/industry
- `scanner/performance.py` — prior CSV vs live quotes
- `scanner/public_client.py` — universe + screener
- `scanner/tui.py` — Textual UI

## Env (names only)

- `PUBLIC_API_SECRET_KEY` — required for universe mode  
- `PUBLIC_COM_ACCOUNT_ID` — required for universe mode  

## Do not

- Commit `.env` or print secrets  
- Revert to screener-only as default without user ask  
- Treat raw 52w-low dumps as stock signals without `--stocks-only`  
