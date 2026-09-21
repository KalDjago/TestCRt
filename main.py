from __future__ import annotations

import csv
import tempfile
from pathlib import Path
from typing import List

import polars as pl
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="CRt Calculation Tool V2", version="0.2.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def normalize(value) -> str:
    return " ".join(str(value or "").strip().upper().split())


def normalize_vin(value) -> str:
    return "".join(str(value or "").strip().upper().split())


async def save_upload(upload: UploadFile) -> str:
    suffix = Path(upload.filename or "upload.bin").suffix.lower()
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
        return handle.name
    finally:
        handle.close()


def read_csv_header(path: str) -> tuple[list[str], int]:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as file:
        reader = csv.reader(file)
        header = next(reader, [])
        rows = sum(1 for _ in reader)
    return header, rows


def read_excel_header(path: str) -> tuple[list[str], int]:
    frame = pl.read_excel(path, engine="calamine")
    return frame.columns, frame.height


def inspect_table(path: str, filename: str) -> tuple[list[str], int]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return read_csv_header(path)
    if suffix in {".xlsx", ".xls", ".xlsb"}:
        return read_excel_header(path)
    raise ValueError("Supported formats: CSV, XLSX, XLS, XLSB")


def extract_year(value) -> int | None:
    if value is None:
        return None
    if hasattr(value, "year"):
        try:
            return int(value.year)
        except Exception:
            pass
    text = str(value).strip()
    for token in text.replace("/", "-").split("-"):
        if len(token) == 4 and token.isdigit() and 1900 <= int(token) <= 2100:
            return int(token)
    for index in range(max(0, len(text) - 3)):
        token = text[index:index + 4]
        if token.isdigit() and 1900 <= int(token) <= 2100:
            return int(token)
    try:
        numeric = float(value)
        if 1900 <= numeric <= 2100:
            return int(numeric)
        if 20000 <= numeric <= 80000:
            date = pl.Series([numeric]).cast(pl.Date, strict=False)[0]
            return int(date.year) if date else None
    except Exception:
        pass
    return None


def load_master(path: str, filename: str, vin_col: str, date_col: str, year_col: str, origin_col: str, crt_year: int, origin_filter: str):
    suffix = Path(filename).suffix.lower()
    required = [vin_col, origin_col, year_col or date_col]
    if suffix == ".csv":
        rows = []
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as file:
            reader = csv.DictReader(file)
            missing = [column for column in required if column not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"Master missing columns: {', '.join(missing)}")
            for row in reader:
                rows.append({column: row.get(column, "") for column in required})
        frame = pl.DataFrame(rows)
    else:
        frame = pl.read_excel(path, engine="calamine")
        missing = [column for column in required if column not in frame.columns]
        if missing:
            raise ValueError(f"Master missing columns: {', '.join(missing)}")
        frame = frame.select(required)

    master: dict[str, dict] = {}
    duplicate_count = 0
    invalid_year = 0
    blank_vin = 0
    normalized_filter = normalize(origin_filter)

    for row in frame.iter_rows(named=True):
        vin = normalize_vin(row.get(vin_col))
        if not vin:
            blank_vin += 1
            continue
        if vin in master:
            duplicate_count += 1
            continue
        year = extract_year(row.get(year_col) if year_col else row.get(date_col))
        if year is None:
            invalid_year += 1
            continue
        age = crt_year - year
        if age < 1 or age > 8:
            continue
        origin = normalize(row.get(origin_col))
        if not origin:
            continue
        if normalized_filter and origin != normalized_filter:
            continue
        master[vin] = {"age": age, "sales_year": year, "origin": origin, "total": 0, "same": 0}

    audit = {
        "master_rows": frame.height,
        "eligible_unique_vin": len(master),
        "duplicate_master_vin": duplicate_count,
        "blank_master_vin": blank_vin,
        "invalid_sales_year": invalid_year,
    }
    return master, audit


def process_achievement_csv(path: str, vin_col: str, destination_col: str, master: dict[str, dict]):
    row_count = blank_vin = matched_rows = 0
    matched_vins: set[str] = set()
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames or []
        missing = [column for column in [vin_col, destination_col] if column not in fields]
        if missing:
            raise ValueError(f"Achievement missing columns: {', '.join(missing)}")
        for row in reader:
            row_count += 1
            vin = normalize_vin(row.get(vin_col))
            if not vin:
                blank_vin += 1
                continue
            record = master.get(vin)
            if record is None:
                continue
            matched_rows += 1
            matched_vins.add(vin)
            record["total"] = 1
            if normalize(row.get(destination_col)) == record["origin"]:
                record["same"] = 1
    return row_count, blank_vin, matched_rows, matched_vins


def process_achievement_excel(path: str, vin_col: str, destination_col: str, master: dict[str, dict]):
    frame = pl.read_excel(path, engine="calamine")
    missing = [column for column in [vin_col, destination_col] if column not in frame.columns]
    if missing:
        raise ValueError(f"Achievement missing columns: {', '.join(missing)}")
    frame = frame.select([vin_col, destination_col])
    row_count = blank_vin = matched_rows = 0
    matched_vins: set[str] = set()
    for row in frame.iter_rows(named=True):
        row_count += 1
        vin = normalize_vin(row.get(vin_col))
        if not vin:
            blank_vin += 1
            continue
        record = master.get(vin)
        if record is None:
            continue
        matched_rows += 1
        matched_vins.add(vin)
        record["total"] = 1
        if normalize(row.get(destination_col)) == record["origin"]:
            record["same"] = 1
    del frame
    return row_count, blank_vin, matched_rows, matched_vins


@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


@app.post("/api/inspect")
async def inspect_file(file: UploadFile = File(...)):
    path = await save_upload(file)
    try:
        columns, rows = inspect_table(path, file.filename or "")
        return {"filename": file.filename, "rows": rows, "columns": columns}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        Path(path).unlink(missing_ok=True)


@app.post("/api/calculate")
async def calculate(
    master_file: UploadFile = File(...),
    achievement_files: List[UploadFile] = File(...),
    crt_year: int = Form(...),
    master_vin: str = Form(...),
    sales_date: str = Form(""),
    sales_year: str = Form(""),
    origin_column: str = Form(...),
    achievement_vin: str = Form(...),
    destination_column: str = Form(...),
    origin_filter: str = Form(""),
    calculation_level: str = Form("Outlet"),
):
    paths: list[str] = []
    try:
        master_path = await save_upload(master_file)
        paths.append(master_path)
        master, audit = load_master(
            master_path, master_file.filename or "", master_vin, sales_date,
            sales_year, origin_column, crt_year, origin_filter
        )
        if not master:
            raise ValueError("No eligible unique VIN found for cohort 1Y-8Y. Check sales date/year and filter.")

        total_achievement_rows = 0
        blank_achievement_vin = 0
        matched_service_rows = 0
        all_matched_vins: set[str] = set()

        for upload in achievement_files:
            path = await save_upload(upload)
            paths.append(path)
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix == ".csv":
                rows, blanks, matches, matched_vins = process_achievement_csv(
                    path, achievement_vin, destination_column, master
                )
            elif suffix in {".xlsx", ".xls", ".xlsb"}:
                rows, blanks, matches, matched_vins = process_achievement_excel(
                    path, achievement_vin, destination_column, master
                )
            else:
                raise ValueError(f"Unsupported achievement type: {suffix}")
            total_achievement_rows += rows
            blank_achievement_vin += blanks
            matched_service_rows += matches
            all_matched_vins.update(matched_vins)
            Path(path).unlink(missing_ok=True)
            paths.remove(path)

        summary = []
        for age in range(1, 9):
            cohort = [record for record in master.values() if record["age"] == age]
            uio = len(cohort)
            same_count = sum(record["same"] for record in cohort)
            total_count = sum(record["total"] for record in cohort)
            summary.append({
                "age": age,
                "sales_year": crt_year - age,
                "uio": uio,
                "same_count": same_count,
                "total_count": total_count,
                "same_rate": same_count / uio if uio else 0,
                "total_rate": total_count / uio if uio else 0,
                "gap": (total_count - same_count) / uio if uio else 0,
            })

        total_uio = len(master)
        total_same = sum(record["same"] for record in master.values())
        total_retained = sum(record["total"] for record in master.values())
        audit.update({
            "achievement_rows_processed": total_achievement_rows,
            "blank_achievement_vin": blank_achievement_vin,
            "matched_service_rows": matched_service_rows,
            "matched_unique_vin": len(all_matched_vins),
            "unmatched_master_vin": total_uio - len(all_matched_vins),
            "files_processed": len(achievement_files),
        })
        return {
            "level": calculation_level,
            "summary": summary,
            "overall": {
                "uio": total_uio,
                "same_count": total_same,
                "total_count": total_retained,
                "same_rate": total_same / total_uio if total_uio else 0,
                "total_rate": total_retained / total_uio if total_uio else 0,
            },
            "audit": audit,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")
    finally:
        for path in paths:
            Path(path).unlink(missing_ok=True)
