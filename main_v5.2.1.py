from __future__ import annotations

import csv
import tempfile
import time
from pathlib import Path
from typing import List, Optional

import polars as pl
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="TAM CRt Calculation Tool V5.2.1", version="0.5.2.1")
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


def read_selected_table(path: str, filename: str, required: list[str]) -> pl.DataFrame:
    suffix = Path(filename).suffix.lower()
    required = list(dict.fromkeys(required))
    if suffix == ".csv":
        rows = []
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as file:
            reader = csv.DictReader(file)
            missing = [column for column in required if column not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"{filename} missing column(s): {', '.join(missing)}")
            for row in reader:
                rows.append({column: row.get(column, "") for column in required})
        return pl.DataFrame(rows)
    if suffix in {".xlsx", ".xls", ".xlsb"}:
        frame = pl.read_excel(path, engine="calamine")
        missing = [column for column in required if column not in frame.columns]
        if missing:
            raise ValueError(f"{filename} missing column(s): {', '.join(missing)}")
        return frame.select(required)
    raise ValueError(f"Unsupported file type: {suffix}")


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
            import datetime as dt
            return (dt.datetime(1899, 12, 30) + dt.timedelta(days=numeric)).year
    except Exception:
        pass
    return None


def load_outlet_reference(
    path: str,
    filename: str,
    code_col: str,
    name_col: str,
    area_col: str,
):
    if not area_col or (not code_col and not name_col):
        raise ValueError("Master Outlet requires Area and at least Outlet Code or Outlet Name.")
    required = [area_col] + ([code_col] if code_col else []) + ([name_col] if name_col else [])
    frame = read_selected_table(path, filename, required)
    by_code: dict[str, str] = {}
    by_name: dict[str, str] = {}
    duplicate_code = conflicting_code = duplicate_name = conflicting_name = invalid_rows = 0
    area_values: set[str] = set()

    for row in frame.iter_rows(named=True):
        code = normalize(row.get(code_col)) if code_col else ""
        name = normalize(row.get(name_col)) if name_col else ""
        area = normalize(row.get(area_col))
        if not area or (not code and not name):
            invalid_rows += 1
            continue
        area_values.add(area)
        if code:
            if code in by_code:
                duplicate_code += 1
                if by_code[code] != area:
                    conflicting_code += 1
            else:
                by_code[code] = area
        if name:
            if name in by_name:
                duplicate_name += 1
                if by_name[name] != area:
                    conflicting_name += 1
            else:
                by_name[name] = area

    if conflicting_code:
        raise ValueError(
            f"Master Outlet has {conflicting_code} conflicting Outlet Code mapping(s). Fix them before Area calculation."
        )
    if not by_code and not by_name:
        raise ValueError("Master Outlet did not produce any valid outlet-to-area mapping.")

    audit = {
        "outlet_master_rows": frame.height,
        "valid_outlet_codes": len(by_code),
        "valid_outlet_names": len(by_name),
        "duplicate_outlet_code": duplicate_code,
        "conflicting_outlet_code": conflicting_code,
        "duplicate_outlet_name": duplicate_name,
        "conflicting_outlet_name": conflicting_name,
        "invalid_outlet_master_rows": invalid_rows,
        "area_categories": len(area_values),
    }
    return by_code, by_name, audit


def lookup_area(code_value, name_value, by_code: dict[str, str], by_name: dict[str, str]):
    code = normalize(code_value)
    if code and code in by_code:
        return by_code[code], "CODE"
    name = normalize(name_value)
    if name and name in by_name:
        return by_name[name], "NAME"
    return "", "UNMAPPED"


def _validate_csv_columns(reader: csv.DictReader, filename: str, required: list[str]):
    missing = [column for column in required if column not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"{filename} missing column(s): {', '.join(missing)}")


def load_master_standard(path, filename, vin_col, date_col, year_col, origin_col, crt_year, origin_filter):
    required = list(dict.fromkeys([vin_col, origin_col, year_col or date_col]))
    if Path(filename).suffix.lower() != ".csv":
        frame = read_selected_table(path, filename, required)
        return build_master(frame, filename, vin_col, date_col, year_col, origin_col, crt_year, origin_filter)

    master: dict[str, dict] = {}
    duplicate_count = invalid_year = blank_vin = blank_origin = outside_cohort = row_count = 0
    normalized_filter = normalize(origin_filter)
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as file:
        reader = csv.DictReader(file)
        _validate_csv_columns(reader, filename, required)
        for row in reader:
            row_count += 1
            vin = normalize_vin(row.get(vin_col))
            if not vin:
                blank_vin += 1
                continue
            # Preserve V5.2 behavior: duplicate is evaluated before year, cohort, and origin filtering.
            if vin in master:
                duplicate_count += 1
                continue
            sales_year_value = extract_year(row.get(year_col) if year_col else row.get(date_col))
            if sales_year_value is None:
                invalid_year += 1
                continue
            age = crt_year - sales_year_value
            if age < 1 or age > 8:
                outside_cohort += 1
                continue
            origin = normalize(row.get(origin_col))
            if not origin:
                blank_origin += 1
                continue
            if normalized_filter and origin != normalized_filter:
                continue
            master[vin] = {"age": age, "sales_year": sales_year_value, "origin": origin, "total": 0, "same": 0}
    audit = {
        "file_name": filename,
        "master_rows": row_count,
        "eligible_unique_vin": len(master),
        "duplicate_master_vin": duplicate_count,
        "blank_master_vin": blank_vin,
        "blank_sales_origin": blank_origin,
        "invalid_sales_year": invalid_year,
        "outside_cohort_vin": outside_cohort,
        "unmapped_sales_outlet_vin": 0,
    }
    return master, audit


def build_master(frame, filename, vin_col, date_col, year_col, origin_col, crt_year, origin_filter):
    master: dict[str, dict] = {}
    duplicate_count = invalid_year = blank_vin = blank_origin = outside_cohort = 0
    normalized_filter = normalize(origin_filter)
    for row in frame.iter_rows(named=True):
        vin = normalize_vin(row.get(vin_col))
        if not vin:
            blank_vin += 1
            continue
        if vin in master:
            duplicate_count += 1
            continue
        sales_year_value = extract_year(row.get(year_col) if year_col else row.get(date_col))
        if sales_year_value is None:
            invalid_year += 1
            continue
        age = crt_year - sales_year_value
        if age < 1 or age > 8:
            outside_cohort += 1
            continue
        origin = normalize(row.get(origin_col))
        if not origin:
            blank_origin += 1
            continue
        if normalized_filter and origin != normalized_filter:
            continue
        master[vin] = {"age": age, "sales_year": sales_year_value, "origin": origin, "total": 0, "same": 0}
    audit = {
        "file_name": filename,
        "master_rows": frame.height,
        "eligible_unique_vin": len(master),
        "duplicate_master_vin": duplicate_count,
        "blank_master_vin": blank_vin,
        "blank_sales_origin": blank_origin,
        "invalid_sales_year": invalid_year,
        "outside_cohort_vin": outside_cohort,
        "unmapped_sales_outlet_vin": 0,
    }
    return master, audit


def load_master_area(
    path, filename, vin_col, date_col, year_col, outlet_code_col, outlet_name_col,
    crt_year, origin_filter, by_code, by_name,
):
    if not outlet_code_col and not outlet_name_col:
        raise ValueError("Area calculation requires Sales Outlet Code or Sales Outlet Name.")
    required = [vin_col, year_col or date_col]
    if outlet_code_col:
        required.append(outlet_code_col)
    if outlet_name_col:
        required.append(outlet_name_col)
    required = list(dict.fromkeys(required))

    if Path(filename).suffix.lower() != ".csv":
        frame = read_selected_table(path, filename, required)
        rows = frame.iter_rows(named=True)
        row_total = frame.height
        return _build_master_area_rows(
            rows, row_total, filename, vin_col, date_col, year_col, outlet_code_col,
            outlet_name_col, crt_year, origin_filter, by_code, by_name,
        )

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as file:
        reader = csv.DictReader(file)
        _validate_csv_columns(reader, filename, required)
        # The helper consumes the reader directly and therefore never creates a full CSV list/DataFrame.
        return _build_master_area_rows(
            reader, None, filename, vin_col, date_col, year_col, outlet_code_col,
            outlet_name_col, crt_year, origin_filter, by_code, by_name,
        )


def _build_master_area_rows(
    rows, known_row_total, filename, vin_col, date_col, year_col, outlet_code_col,
    outlet_name_col, crt_year, origin_filter, by_code, by_name,
):
    master: dict[str, dict] = {}
    duplicate_count = invalid_year = blank_vin = outside_cohort = unmapped = row_count = 0
    normalized_filter = normalize(origin_filter)
    for row in rows:
        row_count += 1
        vin = normalize_vin(row.get(vin_col))
        if not vin:
            blank_vin += 1
            continue
        if vin in master:
            duplicate_count += 1
            continue
        sales_year_value = extract_year(row.get(year_col) if year_col else row.get(date_col))
        if sales_year_value is None:
            invalid_year += 1
            continue
        age = crt_year - sales_year_value
        if age < 1 or age > 8:
            outside_cohort += 1
            continue
        area, _ = lookup_area(
            row.get(outlet_code_col) if outlet_code_col else "",
            row.get(outlet_name_col) if outlet_name_col else "",
            by_code, by_name,
        )
        if not area:
            unmapped += 1
            continue
        if normalized_filter and area != normalized_filter:
            continue
        master[vin] = {"age": age, "sales_year": sales_year_value, "origin": area, "total": 0, "same": 0}
    audit = {
        "file_name": filename,
        "master_rows": known_row_total if known_row_total is not None else row_count,
        "eligible_unique_vin": len(master),
        "duplicate_master_vin": duplicate_count,
        "blank_master_vin": blank_vin,
        "blank_sales_origin": 0,
        "invalid_sales_year": invalid_year,
        "outside_cohort_vin": outside_cohort,
        "unmapped_sales_outlet_vin": unmapped,
    }
    return master, audit

def merge_master(combined, incoming):
    cross_file_duplicates = conflicting_origin_vin = 0
    for vin, record in incoming.items():
        existing = combined.get(vin)
        if existing is None:
            combined[vin] = record
            continue
        cross_file_duplicates += 1
        if existing["origin"] != record["origin"] or existing["sales_year"] != record["sales_year"]:
            conflicting_origin_vin += 1
    return cross_file_duplicates, conflicting_origin_vin


def process_achievement_standard_csv(path, vin_col, destination_col, master):
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


def process_achievement_standard_excel(path, vin_col, destination_col, master):
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
    return row_count, blank_vin, matched_rows, matched_vins


def process_achievement_area_csv(path, vin_col, outlet_code_col, outlet_name_col, master, by_code, by_name):
    required = [vin_col] + ([outlet_code_col] if outlet_code_col else []) + ([outlet_name_col] if outlet_name_col else [])
    row_count = blank_vin = matched_rows = 0
    matched_vins: set[str] = set()
    mapped_vins: set[str] = set()
    unmapped_vins: set[str] = set()
    unmapped_outlets: set[str] = set()
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames or []
        missing = [column for column in required if column not in fields]
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
            area, _ = lookup_area(
                row.get(outlet_code_col) if outlet_code_col else "",
                row.get(outlet_name_col) if outlet_name_col else "",
                by_code, by_name,
            )
            if not area:
                unmapped_vins.add(vin)
                raw_identifier = normalize(row.get(outlet_code_col)) if outlet_code_col else ""
                if not raw_identifier and outlet_name_col:
                    raw_identifier = normalize(row.get(outlet_name_col))
                if raw_identifier:
                    unmapped_outlets.add(raw_identifier)
                continue
            mapped_vins.add(vin)
            if area == record["origin"]:
                record["same"] = 1
    return row_count, blank_vin, matched_rows, matched_vins, mapped_vins, unmapped_vins, unmapped_outlets


def process_achievement_area_excel(path, vin_col, outlet_code_col, outlet_name_col, master, by_code, by_name):
    required = [vin_col] + ([outlet_code_col] if outlet_code_col else []) + ([outlet_name_col] if outlet_name_col else [])
    frame = read_selected_table(path, "achievement.xlsx", required)
    row_count = blank_vin = matched_rows = 0
    matched_vins: set[str] = set()
    mapped_vins: set[str] = set()
    unmapped_vins: set[str] = set()
    unmapped_outlets: set[str] = set()
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
        area, _ = lookup_area(
            row.get(outlet_code_col) if outlet_code_col else "",
            row.get(outlet_name_col) if outlet_name_col else "",
            by_code, by_name,
        )
        if not area:
            unmapped_vins.add(vin)
            raw_identifier = normalize(row.get(outlet_code_col)) if outlet_code_col else ""
            if not raw_identifier and outlet_name_col:
                raw_identifier = normalize(row.get(outlet_name_col))
            if raw_identifier:
                unmapped_outlets.add(raw_identifier)
            continue
        mapped_vins.add(vin)
        if area == record["origin"]:
            record["same"] = 1
    return row_count, blank_vin, matched_rows, matched_vins, mapped_vins, unmapped_vins, unmapped_outlets


@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.5.2.1", "engine": "V5.2 core plus streaming Master CSV memory patch"}


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
    master_files: List[UploadFile] = File(...),
    achievement_files: List[UploadFile] = File(...),
    outlet_master_file: Optional[UploadFile] = File(None),
    crt_year: int = Form(...),
    master_vin: str = Form(...),
    sales_date: str = Form(""),
    sales_year: str = Form(""),
    origin_column: str = Form(""),
    achievement_vin: str = Form(...),
    destination_column: str = Form(""),
    origin_filter: str = Form(""),
    calculation_level: str = Form("Outlet"),
    master_outlet_code: str = Form(""),
    master_outlet_name: str = Form(""),
    achievement_outlet_code: str = Form(""),
    achievement_outlet_name: str = Form(""),
    reference_outlet_code: str = Form(""),
    reference_outlet_name: str = Form(""),
    reference_area: str = Form(""),
):
    started_at = time.perf_counter()
    paths: list[str] = []
    level = normalize(calculation_level)
    area_mode = level == "AREA"

    try:
        by_code: dict[str, str] = {}
        by_name: dict[str, str] = {}
        outlet_audit = {
            "outlet_master_rows": 0,
            "valid_outlet_codes": 0,
            "valid_outlet_names": 0,
            "duplicate_outlet_code": 0,
            "conflicting_outlet_code": 0,
            "duplicate_outlet_name": 0,
            "conflicting_outlet_name": 0,
            "invalid_outlet_master_rows": 0,
            "area_categories": 0,
        }

        if area_mode:
            if outlet_master_file is None or not outlet_master_file.filename:
                raise ValueError("Master Outlet file is required for Area calculation.")
            if not achievement_outlet_code and not achievement_outlet_name:
                raise ValueError("Area calculation requires Achievement Outlet Code or Outlet Name.")
            outlet_path = await save_upload(outlet_master_file)
            paths.append(outlet_path)
            by_code, by_name, outlet_audit = load_outlet_reference(
                outlet_path,
                outlet_master_file.filename or "",
                reference_outlet_code,
                reference_outlet_name,
                reference_area,
            )
            Path(outlet_path).unlink(missing_ok=True)
            paths.remove(outlet_path)
        else:
            if not origin_column or not destination_column:
                raise ValueError("Sales Origin and Service Destination are required.")

        master: dict[str, dict] = {}
        master_file_audits: list[dict] = []
        cross_file_duplicates = conflicting_origin_vin = 0

        for upload in master_files:
            path = await save_upload(upload)
            paths.append(path)
            if area_mode:
                current_master, file_audit = load_master_area(
                    path, upload.filename or "", master_vin, sales_date, sales_year,
                    master_outlet_code, master_outlet_name, crt_year, origin_filter, by_code, by_name,
                )
            else:
                current_master, file_audit = load_master_standard(
                    path, upload.filename or "", master_vin, sales_date, sales_year,
                    origin_column, crt_year, origin_filter,
                )
            duplicate_increment, conflict_increment = merge_master(master, current_master)
            cross_file_duplicates += duplicate_increment
            conflicting_origin_vin += conflict_increment
            master_file_audits.append(file_audit)
            Path(path).unlink(missing_ok=True)
            paths.remove(path)

        if not master:
            raise ValueError("No eligible unique VIN found. Check mappings, CRt year, Origin Filter, and Master Outlet coverage.")

        total_achievement_rows = blank_achievement_vin = matched_service_rows = 0
        all_matched_vins: set[str] = set()
        all_mapped_vins: set[str] = set()
        all_unmapped_vins: set[str] = set()
        all_unmapped_outlets: set[str] = set()

        for upload in achievement_files:
            path = await save_upload(upload)
            paths.append(path)
            suffix = Path(upload.filename or "").suffix.lower()
            if area_mode:
                processor = process_achievement_area_csv if suffix == ".csv" else process_achievement_area_excel
                rows, blanks, matches, matched_vins, mapped_vins, unmapped_vins, unmapped_outlets = processor(
                    path, achievement_vin, achievement_outlet_code, achievement_outlet_name,
                    master, by_code, by_name,
                )
                all_mapped_vins.update(mapped_vins)
                all_unmapped_vins.update(unmapped_vins)
                all_unmapped_outlets.update(unmapped_outlets)
            else:
                if suffix == ".csv":
                    rows, blanks, matches, matched_vins = process_achievement_standard_csv(
                        path, achievement_vin, destination_column, master
                    )
                elif suffix in {".xlsx", ".xls", ".xlsb"}:
                    rows, blanks, matches, matched_vins = process_achievement_standard_excel(
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
        mapped_unique = len(all_mapped_vins)
        unmapped_unique = len(all_unmapped_vins - all_mapped_vins)
        mapping_denominator = len(all_matched_vins)
        mapping_rate = mapped_unique / mapping_denominator if mapping_denominator else 1.0

        audit = {
            "master_files_processed": len(master_files),
            "master_rows": sum(item["master_rows"] for item in master_file_audits),
            "eligible_unique_vin": total_uio,
            "duplicate_master_vin": sum(item["duplicate_master_vin"] for item in master_file_audits),
            "cross_file_duplicate_vin": cross_file_duplicates,
            "conflicting_origin_vin": conflicting_origin_vin,
            "invalid_sales_year": sum(item["invalid_sales_year"] for item in master_file_audits),
            "outside_cohort_vin": sum(item["outside_cohort_vin"] for item in master_file_audits),
            "unmapped_sales_outlet_vin": sum(item["unmapped_sales_outlet_vin"] for item in master_file_audits),
            "achievement_files_processed": len(achievement_files),
            "achievement_rows_processed": total_achievement_rows,
            "matched_unique_vin": len(all_matched_vins),
            "unmatched_master_vin": total_uio - len(all_matched_vins),
            "blank_achievement_vin": blank_achievement_vin,
            "matched_service_rows": matched_service_rows,
            "backend_processing_seconds": round(time.perf_counter() - started_at, 2),
            "mapped_service_vin": mapped_unique if area_mode else 0,
            "unmapped_service_vin": unmapped_unique if area_mode else 0,
            "unique_unmapped_service_outlets": len(all_unmapped_outlets) if area_mode else 0,
            "outlet_mapping_rate": mapping_rate if area_mode else 1.0,
            **outlet_audit,
        }

        warnings = []
        if area_mode and audit["unmapped_sales_outlet_vin"]:
            warnings.append(
                f'{audit["unmapped_sales_outlet_vin"]:,} Master VIN were excluded because Sales Outlet could not be mapped to Area.'
            )
        if area_mode and unmapped_unique:
            warnings.append(
                f"{unmapped_unique:,} matched VIN had an unmapped Service Outlet. Area Total remains valid, but Same Area may be understated."
            )
        if area_mode and outlet_audit["conflicting_outlet_name"]:
            warnings.append(
                f'{outlet_audit["conflicting_outlet_name"]:,} duplicate Outlet Name mapping(s) have conflicting Area values. Code matching remains prioritized.'
            )

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
            "warnings": warnings,
            "mapping_status": "WARNING" if warnings else "PASS",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")
    finally:
        for path in paths:
            Path(path).unlink(missing_ok=True)
