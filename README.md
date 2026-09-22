# TAM CRt Analytics V4 Hybrid

Memory-first calculation engine inspired by V2, with persistent Master Outlet and summary-only Data Bank.

## Key design
- Working VINs stay in application memory, not SQLite.
- SQLite Volume stores only Master Outlet and finalized summary results.
- Multi-select Master and Achievement files are uploaded sequentially by the browser.
- Population filter supports All, Area, Dealer, and Outlet.
- Saving to Data Bank clears working VIN memory by default.
- Light/dark mode and Toyota/TAM-inspired UI.

## Railway
Mount Volume at `/app/data` and set:
`DB_PATH=/app/data/crt_v4.db`

Unfinished sessions are intentionally cleared on redeploy. Saved results and Master Outlet persist.
