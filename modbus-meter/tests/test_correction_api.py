"""/correct 上传接口集成测试（httpx ASGI 传输，不依赖网络与 Docker）。"""

from __future__ import annotations

import csv
import io

import httpx
import pytest

from app.main import app

CAL = (
    "boot_id,boot_ms,utc_ms\n"
    "1,1000,1700000001000\n"
    "1,61000,1700000060000\n"
    "2,500,1700000180500\n"
    "2,60500,1700000240500\n"
)

READINGS = (
    "meter_id,boot_id,boot_ms,word_high,word_low\n"
    "1001,1,1000,0,100\n"          # 行2 -> 1700000001，正常
    "1001,2,10500,0,102\n"         # 行3 -> 1700000190
    "1001,1,31000,1,134\n"         # 行4 -> 1700000030，正常
    "1001,2,10500,0,5\n"           # 行5 -> 1700000190，与行3 跨/同秒撞车
    "1005,9,1000,0,7\n"            # 行6 -> 未知启动，待核对
    "1001,1,500,0,99\n"            # 行7 -> 区间外，待核对
)


async def _post(cal: bytes, readings: bytes):
    transport = httpx.ASGITransport(app=app)
    files = {
        "calibration": ("calibration_sample.csv", cal, "text/csv"),
        "readings": ("readings_sample.csv", readings, "text/csv"),
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.post("/correct", files=files)


async def test_correct_happy_path_returns_csv_in_original_order():
    resp = await _post(CAL.encode(), READINGS.encode())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "corrected_readings.csv" in resp.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert [r["source_row"] for r in rows] == ["2", "3", "4", "5", "6", "7"]
    by_line = {r["source_row"]: r for r in rows}

    assert by_line["2"]["status"] == "已校正"
    assert by_line["2"]["corrected_second"] == "1700000001"
    assert by_line["2"]["total_count"] == "100"
    assert by_line["4"]["total_count"] == str((1 << 16) + 134)
    assert by_line["4"]["corrected_second"] == "1700000030"

    # 撞秒两笔全部待核对
    for line in ("3", "5"):
        assert by_line[line]["status"] == "待核对"
        assert by_line[line]["corrected_second"] == "1700000190"
        assert "同表同秒" in by_line[line]["review_reason"]

    # 无法换算：校正秒留空
    assert by_line["6"]["status"] == "待核对"
    assert by_line["6"]["corrected_second"] == ""
    assert "无校时数据" in by_line["6"]["review_reason"]
    assert by_line["7"]["status"] == "待核对"
    assert "覆盖区间" in by_line["7"]["review_reason"]


async def test_correct_bad_row_returns_422_with_filename_and_line():
    bad = READINGS + "1001,1,2000,70000,1\n"  # word_high 超 16 位，物理行 8
    resp = await _post(CAL.encode(), bad.encode())
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "readings_sample.csv" in detail
    assert "第 8 行" in detail
    # 失败不产出部分结果：响应是 JSON 错误而非 CSV
    assert "text/csv" not in resp.headers.get("content-type", "")


async def test_correct_bad_calibration_reports_its_filename():
    bad_cal = CAL + "1,12000,1700000000000\n"  # utc_ms 倒退
    resp = await _post(bad_cal.encode(), READINGS.encode())
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "calibration_sample.csv" in detail
    assert "UTC 毫秒不严格递增" in detail


async def test_correct_missing_file_returns_422():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post(
            "/correct",
            files={"calibration": ("c.csv", CAL.encode(), "text/csv")},
        )
    assert resp.status_code == 422
