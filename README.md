# TAM CRt Calculation Tool V5

A conservative upgrade of the proven V2 calculation tool.

## Included
- Original V2 Same versus Total calculation logic
- Multi-upload Master UIO with one-file compatibility
- Existing multi-upload Achievement flow
- Cross-file duplicate and origin-conflict audit
- Backend processing time and total browser elapsed time
- Toyota/TAM-inspired light and dark visual theme

## Deliberately excluded
- Data Bank
- Export
- Progress engine
- Database and Railway Volume dependency
- Master Outlet enrichment
- Persistent working sessions

## Railway start command
`uvicorn main:app --host 0.0.0.0 --port $PORT`
