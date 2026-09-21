# CRt Calculation Tool V2

Memory-efficient FastAPI tool for CRt Same vs Total at Area, Dealer, or Outlet level.

## Railway start command

`uvicorn main:app --host 0.0.0.0 --port $PORT`

## Recommended input

- Master UIO: XLSX or CSV
- Achievement: CSV for large files
- Required master fields: VIN, sales date/year, origin Area/Dealer/Outlet
- Required achievement fields: VIN, service destination Area/Dealer/Outlet

Achievement CSV files are processed sequentially, one row at a time. Only VINs that exist in eligible master UIO are retained in memory.
