
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import List

import polars as pl
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="CRt Calculation Tool", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def norm_name(value: str) -> str:
    return " ".join(str(value).strip().upper().split())


def normalize_vin_expr(column: str) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.Utf8, strict=False)
        .fill_null("")
        .str.strip_chars()
        .str.to_uppercase()
        .str.replace_all(r"\s+", "")
    )


def normalize_text_expr(column: str) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.Utf8, strict=False)
        .fill_null("")
        .str.strip_chars()
        .str.to_uppercase()
        .str.replace_all(r"\s+", " ")
    )


def read_table(path: str, original_name: str) -> pl.DataFrame:
    suffix = Path(original_name).suffix.lower()
    if suffix == ".csv":
        try:
            return pl.read_csv(path, infer_schema_length=10000, ignore_errors=True)
        except Exception:
            return pl.read_csv(path, encoding="utf8-lossy", infer_schema_length=10000, ignore_errors=True)
    if suffix in {".xlsx", ".xls", ".xlsb"}:
        return pl.read_excel(path, engine="calamine")
    raise ValueError(f"Unsupported file type: {suffix}")


async def save_upload(upload: UploadFile) -> str:
    suffix = Path(upload.filename or "upload.bin").suffix
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        while chunk := await upload.read(1024 * 1024):
            handle.write(chunk)
        return handle.name
    finally:
        handle.close()


def require_columns(df: pl.DataFrame, required: list[str], label: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{label} missing column(s): {', '.join(missing)}")


def build_result(
    master: pl.DataFrame,
    achievement: pl.DataFrame,
    crt_year: int,
    master_vin: str,
    sales_date: str,
    sales_year: str,
    sales_area: str,
    achievement_vin: str,
    service_area: str,
    selected_area: str,
):
    require_columns(master, [master_vin, sales_area], "Master")
    if sales_year:
        require_columns(master, [sales_year], "Master")
    elif sales_date:
        require_columns(master, [sales_date], "Master")
    else:
        raise ValueError("Choose Sales Year or Sales Date.")
    require_columns(achievement, [achievement_vin, service_area], "Achievement")

    if sales_year:
        year_expr = (
            pl.col(sales_year)
            .cast(pl.Utf8, strict=False)
            .str.extract(r"((?:19|20)\d{2})", 1)
            .cast(pl.Int32, strict=False)
        )
    else:
        # Handles date strings, timestamp strings, and Excel date values that Polars parsed as dates.
        raw = pl.col(sales_date)
        year_expr = (
            pl.when(raw.cast(pl.Date, strict=False).is_not_null())
            .then(raw.cast(pl.Date, strict=False).dt.year())
            .otherwise(
                raw.cast(pl.Utf8, strict=False)
                .str.extract(r"((?:19|20)\d{2})", 1)
                .cast(pl.Int32, strict=False)
            )
        )

    master_clean = (
        master
        .with_columns([
            normalize_vin_expr(master_vin).alias("VIN_KEY"),
            normalize_text_expr(sales_area).alias("SALES_AREA_KEY"),
            pl.col(sales_area).cast(pl.Utf8, strict=False).fill_null("").str.strip_chars().alias("SALES_AREA"),
            year_expr.alias("SALES_YEAR"),
        ])
        .filter((pl.col("VIN_KEY") != "") & (pl.col("SALES_AREA_KEY") != "") & pl.col("SALES_YEAR").is_not_null())
        .with_columns((pl.lit(crt_year) - pl.col("SALES_YEAR")).alias("AGE"))
        .filter(pl.col("AGE").is_between(1, 8))
        .unique(subset=["VIN_KEY"], keep="first")
    )

    if selected_area.strip():
        master_clean = master_clean.filter(pl.col("SALES_AREA_KEY") == norm_name(selected_area))

    achievement_clean = (
        achievement
        .with_columns([
            normalize_vin_expr(achievement_vin).alias("VIN_KEY"),
            normalize_text_expr(service_area).alias("SERVICE_AREA_KEY"),
        ])
        .filter(pl.col("VIN_KEY") != "")
    )

    service_flags = (
        achievement_clean
        .group_by("VIN_KEY")
        .agg([
            pl.len().alias("SERVICE_ROW_COUNT"),
            pl.col("SERVICE_AREA_KEY").filter(pl.col("SERVICE_AREA_KEY") != "").unique().alias("SERVICE_AREAS"),
        ])
    )

    detail = (
        master_clean
        .join(service_flags, on="VIN_KEY", how="left")
        .with_columns([
            pl.col("SERVICE_ROW_COUNT").fill_null(0),
            pl.col("SERVICE_AREAS").fill_null(pl.lit([]).cast(pl.List(pl.Utf8))),
        ])
        .with_columns([
            (pl.col("SERVICE_ROW_COUNT") > 0).cast(pl.Int8).alias("AREA_TOTAL_FLAG"),
            pl.struct(["SALES_AREA_KEY", "SERVICE_AREAS"]).map_elements(
                lambda x: int(x["SALES_AREA_KEY"] in (x["SERVICE_AREAS"] or [])),
                return_dtype=pl.Int8,
            ).alias("SAME_AREA_FLAG"),
        ])
        .select([
            pl.col("VIN_KEY").alias("VIN"), "SALES_YEAR", "AGE", "SALES_AREA",
            "SAME_AREA_FLAG", "AREA_TOTAL_FLAG", "SERVICE_ROW_COUNT", "SERVICE_AREAS"
        ])
    )

    summary_raw = (
        detail.group_by("AGE")
        .agg([
            pl.len().alias("UIO"),
            pl.col("SAME_AREA_FLAG").sum().alias("SAME_AREA_VIN"),
            pl.col("AREA_TOTAL_FLAG").sum().alias("AREA_TOTAL_VIN"),
        ])
    )

    ages = pl.DataFrame({"AGE": list(range(1, 9))})
    summary = (
        ages.join(summary_raw, on="AGE", how="left")
        .with_columns([
            pl.col("UIO").fill_null(0),
            pl.col("SAME_AREA_VIN").fill_null(0),
            pl.col("AREA_TOTAL_VIN").fill_null(0),
        ])
        .with_columns([
            (pl.lit(crt_year) - pl.col("AGE")).alias("SALES_YEAR"),
            pl.when(pl.col("UIO") > 0).then(pl.col("SAME_AREA_VIN") / pl.col("UIO")).otherwise(0.0).alias("CRT_SAME_AREA"),
            pl.when(pl.col("UIO") > 0).then(pl.col("AREA_TOTAL_VIN") / pl.col("UIO")).otherwise(0.0).alias("CRT_AREA_TOTAL"),
        ])
        .with_columns((pl.col("CRT_AREA_TOTAL") - pl.col("CRT_SAME_AREA")).alias("GAP"))
        .sort("AGE")
    )

    total_uio = int(summary["UIO"].sum())
    total_same = int(summary["SAME_AREA_VIN"].sum())
    total_area = int(summary["AREA_TOTAL_VIN"].sum())
    overall = {
        "uio": total_uio,
        "same_area_vin": total_same,
        "area_total_vin": total_area,
        "crt_same_area": total_same / total_uio if total_uio else 0,
        "crt_area_total": total_area / total_uio if total_uio else 0,
    }
    return summary, detail, overall


@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/api/inspect")
async def inspect_file(file: UploadFile = File(...)):
    path = await save_upload(file)
    try:
        df = read_table(path, file.filename or "")
        return {"filename": file.filename, "rows": df.height, "columns": df.columns}
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
    sales_area: str = Form(...),
    achievement_vin: str = Form(...),
    service_area: str = Form(...),
    selected_area: str = Form(""),
):
    temp_paths: list[str] = []
    try:
        master_path = await save_upload(master_file)
        temp_paths.append(master_path)
        master = read_table(master_path, master_file.filename or "")

        frames = []
        for upload in achievement_files:
            path = await save_upload(upload)
            temp_paths.append(path)
            frames.append(read_table(path, upload.filename or ""))
        if not frames:
            raise ValueError("Upload at least one achievement file.")
        achievement = pl.concat(frames, how="diagonal_relaxed")

        summary, detail, overall = build_result(
            master, achievement, crt_year, master_vin, sales_date, sales_year,
            sales_area, achievement_vin, service_area, selected_area
        )

        job_dir = Path(tempfile.mkdtemp(prefix="crt_result_"))
        result_file = job_dir / f"CRt_Area_{crt_year}.xlsx"
        with __import__("xlsxwriter").Workbook(result_file) as workbook:
            pct_format = workbook.add_format({"num_format": "0.0%"})
            summary_ws = workbook.add_worksheet("CRt Summary")
            detail_ws = workbook.add_worksheet("VIN Detail")
            summary_headers = summary.columns
            for c, header in enumerate(summary_headers): summary_ws.write(0, c, header)
            for r, row in enumerate(summary.iter_rows(), start=1):
                for c, value in enumerate(row):
                    if summary_headers[c] in {"CRT_SAME_AREA", "CRT_AREA_TOTAL", "GAP"}:
                        summary_ws.write(r, c, value, pct_format)
                    else: summary_ws.write(r, c, value)
            detail_export = detail.with_columns(pl.col("SERVICE_AREAS").list.join(", "))
            for c, header in enumerate(detail_export.columns): detail_ws.write(0, c, header)
            for r, row in enumerate(detail_export.iter_rows(), start=1):
                for c, value in enumerate(row): detail_ws.write(r, c, value)

        token = result_file.as_posix()
        return {
            "summary": summary.to_dicts(),
            "overall": overall,
            "eligible_vin": detail.height,
            "download_token": token,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        for path in temp_paths:
            Path(path).unlink(missing_ok=True)


@app.get("/api/download")
def download(token: str):
    path = Path(token)
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved = path.resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid token")
    if temp_root not in resolved.parents or not resolved.exists() or resolved.suffix != ".xlsx":
        raise HTTPException(status_code=404, detail="Result file not found")
    return FileResponse(resolved, filename=resolved.name)
