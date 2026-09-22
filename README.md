# TAM CRt Analytics Platform V3

## Features
- Sequential multi Master UIO upload
- Master Outlet reference: Outlet to Dealer to CRt Area
- Sequential Achievement upload
- Same Outlet, Same Dealer, Same Area, and Total in one calculation
- CRt 1Y to 8Y
- Data Bank save/view/delete
- Light/Dark mode
- Toyota-inspired internal analytics design

## Railway persistence
For Data Bank persistence, create a Railway Volume mounted at `/data` and add environment variable `DB_PATH=/data/crt_v3.db`.

## Start command
`uvicorn main:app --host 0.0.0.0 --port $PORT`
