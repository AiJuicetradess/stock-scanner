# CURRENT_PROGRESS — stock-scanner

**Last updated:** 2026-08-03  
**Status:** Greenfield v0.1 shipped — full-universe API scan works; TUI + headless + CSV

## Pickup first

1. Run full scan or tune threshold/filters as needed  
2. Optional: mkt-cap / asset-class filters, watchlist alerts  

## Canonical docs

- `README.md` — install, auth, flags, performance  
- `scanner/public_client.py` — universe + screener logic  
- `scanner/tui.py` — Textual UI  

## Env (names only)

- `PUBLIC_API_SECRET_KEY` — required for universe mode  
- `PUBLIC_COM_ACCOUNT_ID` — required for universe mode  

## Do not

- Commit `.env` or print secrets  
- Revert to screener-only as default without user ask  
