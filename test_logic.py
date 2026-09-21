
import polars as pl
from main import build_result

def test_crt_logic():
    master = pl.DataFrame({
        "VIN": ["A", "B", "C", "D", "A"],
        "Sales Year": [2025, 2025, 2024, 2018, 2025],
        "Sales Area": ["AREA 1", "AREA 1", "AREA 1", "AREA 2", "AREA 1"],
    })
    achievement = pl.DataFrame({
        "VIN": ["A", "A", "B", "D"],
        "Service Area": ["AREA 2", "AREA 1", "AREA 2", "AREA 2"],
    })
    summary, detail, overall = build_result(
        master, achievement, 2026, "VIN", "", "Sales Year", "Sales Area",
        "VIN", "Service Area", ""
    )
    one_year = summary.filter(pl.col("AGE") == 1).row(0, named=True)
    assert one_year["UIO"] == 2
    assert one_year["SAME_AREA_VIN"] == 1
    assert one_year["AREA_TOTAL_VIN"] == 2
    assert overall["uio"] == 4
    assert overall["same_area_vin"] == 2
    assert overall["area_total_vin"] == 3
