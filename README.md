# TAM CRt Calculation Tool V5.2.1

Conservative V5 upgrade that adds optional Master Outlet enrichment for Area calculation.

## Unchanged V5 behavior
- Outlet and Dealer use the original V5 direct origin/destination matching.
- Multi-upload Master UIO and Achievement processing remain sequential.
- No database, Data Bank, persistent sessions, or export.

## Area mode
- Master Outlet is required only when Calculation Level is Area.
- Matching priority: Outlet Code, then normalized Outlet Name.
- Master VIN with unmapped Sales Outlet is excluded from eligible Area UIO.
- Matched VIN with unmapped Service Outlet remains in Area Total, but Same Area may be understated and a warning is shown.
- Mapping coverage and unmapped counts are included in Processing Audit.

## Railway
`uvicorn main:app --host 0.0.0.0 --port $PORT`


### V5.2.1 memory patch
- Large Master CSV files are streamed row by row.
- Origin Filter is evaluated before storing non-target VIN records.
- Master CSV no longer creates a full Python row list and Polars DataFrame.
- XLSX/XLS/XLSB behavior is unchanged; CSV remains recommended for very large Master UIO files.
- CRt formulas, Area enrichment, Achievement processing, audit definitions, and UI flow remain unchanged.
