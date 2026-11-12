"""校时 API 与校时算法测试（不依赖 Docker / Modbus 设备）。"""

from __future__ import annotations

import io

import httpx
import pytest

from app import correction as corr
from app.correction import (
    REASON_COLLISION,
    REASON_NO_CALIBRATION,
    REASON_OUT_OF_RANGE,
    STATUS_OK,
    STATUS_REVIEW,
    CalibrationTable,
    CalPoint,
    CorrectionError,
    load_calibration,
    prepare_correction,
)
from app.main import app


# --------------------------------------------------------------- 纯算法


def _table() -> CalibrationTable:
    return CalibrationTable(
        {
            1: [
                CalPoint(uptime_ms=0, utc_ms=1_700_000_000_000),
                CalPoint(uptime_ms=100_000, utc_ms=1_700_000_100_000),
            ],
            2: [
                CalPoint(uptime_ms=0, utc_ms=1_700_010_000_000),
                CalPoint(uptime_ms=100_000, utc_ms=1_700_010_100_000),
            ],
        }
    )


def test_linear_interpolation_uses_integer_floor_second():
    table = _table()
    # 端点含两端
    assert table.correct_second(1, 0) == 1_700_000_000
    assert table.correct_second(1, 100_000) == 1_700_000_100
    # 1:1 映射，50_000ms -> 1_700_000_050_000ms -> 秒 1_700_000_050
    assert table.correct_second(1, 50_000) == 1_700_000_050


def test_floor_second_with_skewed_clock():
    # 走偏后斜率非 1：开机 100_000ms 对应 UTC 100_500ms（快 500ms）
    table = CalibrationTable(
        {1: [CalPoint(0, 0), CalPoint(100_000, 100_500)]}
    )
    # uptime 200ms -> utc 201ms -> 向下取整 0 秒
    assert table.correct_second(1, 200) == 0
    # uptime 99_900ms -> 100_399.5ms -> 100 秒
    assert table.correct_second(1, 99_900) == 100


def test_outside_adjacent_point_range_not_extrapolated():
    table = _table()
    assert table.correct_second(1, 100_001) is None  # 晚于最后点
    assert table.correct_second(9, 0) is None        # 无该启动
    # 恰等于最后一个校时点：端点包含，可换算
    assert table.correct_second(1, 100_000) == 1_700_000_100
    # 恰在第一个点之前 1ms：不换算
    assert table.correct_second(1, -0) == 1_700_000_000


def test_calibration_requires_strict_increase(tmp_path):
    good = tmp_path / "cal.csv"
    good.write_text(
        "boot_id,uptime_ms,utc_ms\n1,0,1000\n1,10,2000\n2,0,500\n",
        encoding="utf-8",
    )
    table = load_calibration(str(good), "cal.csv")
    assert table.correct_second(2, 0) == 0

    bad_uptime = tmp_path / "bad1.csv"
    bad_uptime.write_text(
        "boot_id,uptime_ms,utc_ms\n1,10,1000\n1,10,2000\n", encoding="utf-8"
    )
    with pytest.raises(CorrectionError, match="bad1.csv 第 3 行.*开机毫秒"):
        load_calibration(str(bad_uptime), "bad1.csv")

    bad_utc = tmp_path / "bad2.csv"
    bad_utc.write_text(
        "boot_id,uptime_ms,utc_ms\n1,0,2000\n1,10,2000\n", encoding="utf-8"
    )
    with pytest.raises(CorrectionError, match="bad2.csv 第 3 行.*UTC 毫秒"):
        load_calibration(str(bad_utc), "bad2.csv")


def test_calibration_row_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(corr, "MAX_CALIBRATION_ROWS", 2)
    path = tmp_path / "cal.csv"
    path.write_text(
        "boot_id,uptime_ms,utc_ms\n1,0,1000\n1,10,2000\n1,20,3000\n",
        encoding="utf-8",
    )
    with pytest.raises(CorrectionError, match="第 4 行.*上限"):
        load_calibration(str(path), "cal.csv")


def test_calibration_strict_check_is_per_boot_even_when_interleaved(tmp_path):
    # 输入约定：同一次启动的行连续出现；不同启动穿插时按各自序列独立校验
    path = tmp_path / "cal.csv"
    path.write_text(
        "boot_id,uptime_ms,utc_ms\n"
        "1,0,1000\n2,0,500\n1,10,2000\n2,10,600\n",
        encoding="utf-8",
    )
    table = load_calibration(str(path), "cal.csv")
    assert table.correct_second(1, 5) == 1
    assert table.correct_second(2, 10) == 0  # 600ms 向下取整 0 秒


# --------------------------------------------------------------- 端到端流水线


class _Upload:
    """模拟 FastAPI UploadFile 的最小接口（.filename / .file）。"""

    def __init__(self, content: bytes, filename: str):
        self.filename = filename
        self.file = io.BytesIO(content)


def _job(readings: str, calibration: str):
    return prepare_correction(
        _Upload(readings.encode("utf-8"), "readings.csv"),
        _Upload(calibration.encode("utf-8"), "calibration.csv"),
    )


def _run(readings: str, calibration: str) -> list[list[str]]:
    import csv as _csv

    job = _job(readings, calibration)
    try:
        raw = b"".join(job.stream()).decode("utf-8-sig")
    finally:
        job.close()
    return list(_csv.reader(io.StringIO(raw)))


# 两个启动的 UTC 锚点相同（网关重启后重新对齐 UTC），
# 因此同表在 uptime=0 的读数会形成跨启动撞秒
CAL = (
    "boot_id,uptime_ms,utc_ms\n"
    "1,0,1700000000000\n"
    "1,100000,1700000100000\n"
    "2,0,1700000000000\n"
    "2,100000,1700000100000\n"
)


def test_full_pipeline_statuses_and_original_order():
    readings = (
        "meter_id,boot_id,uptime_ms,word_high,word_low\n"
        "7,1,0,0,100\n"          # 端点 -> 1700000000，与第 5 行跨启动撞秒
        "7,1,50000,0,110\n"      # 插值 -> 1700000050
        "8,1,0,0,5\n"            # 表 8 -> 1700000000，与第 6 行（同表跨启动）撞
        "7,2,0,0,200\n"          # 跨启动同表同秒 1700000000 -> 撞秒
        "8,2,0,0,9\n"            # 与第 3 行同表跨启动撞秒
        "7,1,90000,0,120\n"      # 区间内 -> 1700000090
        "7,3,1000,0,300\n"       # boot 3 无校时
        "65535,1,2500,65535,65535\n"  # 高字在前 -> 4294967295
        "7,1,120000,0,130\n"     # 超出末点 -> 待核对
    )
    rows = _run(readings, CAL)
    assert rows[0] == list(corr.OUTPUT_HEADERS)
    body = rows[1:]
    # 原序保留，源行号 = 物理行号（表头是第 1 行）
    assert [r[0] for r in body] == [str(n) for n in range(2, 11)]
    # 累计计数高字在前
    assert body[7][2] == str(0xFFFFFFFF)
    # 跨启动撞秒：源行 2、5（表 7）与源行 4、6（表 8）全部待核对
    collision_rows = {r[0]: r for r in body if r[5] == REASON_COLLISION}
    assert set(collision_rows) == {"2", "5", "4", "6"}
    assert collision_rows["2"][3] == "1700000000"
    # 未撞秒且可换算的行正常校正
    ok = {r[0]: r for r in body if r[4] == STATUS_OK}
    assert set(ok) == {"3", "7", "9"}
    assert ok["3"][3] == "1700000050"
    assert ok["7"][3] == "1700000090"
    # 无该启动校时
    no_cal = next(r for r in body if r[0] == "8")
    assert no_cal[3] == "" and no_cal[5] == REASON_NO_CALIBRATION
    # 出区间
    out = next(r for r in body if r[0] == "10")
    assert out[3] == "" and out[5] == REASON_OUT_OF_RANGE


def test_same_boot_collision_also_all_review():
    readings = (
        "meter_id,boot_id,uptime_ms,word_high,word_low\n"
        "7,1,1000,0,1\n"
        "7,1,1500,0,2\n"   # 1000ms 与 1500ms 取整后都落在 1700000001 秒
        "7,1,3000,0,3\n"   # -> 1700000003 秒，唯一
    )
    rows = _run(readings, CAL)[1:]
    assert rows[0][3] == rows[1][3] == "1700000001"
    assert rows[0][4] == rows[1][4] == STATUS_REVIEW
    assert rows[2][4] == STATUS_OK
    assert rows[2][3] == "1700000003"


def test_bad_rows_report_filename_and_line_no_partial_results(tmp_path):
    # (读数 CSV 内容, 期望错误消息正则)；上传名固定为 readings.csv
    cases = [
        (
            "meter_id,boot_id,uptime_ms,word_high,word_low\n"
            "7,1,0,0,1\n\n",
            "readings.csv 第 3 行.*空行",
        ),
        (
            "meter_id,boot_id,uptime_ms,word_high,word_low\n"
            "7,1,0,70000,1\n",
            "readings.csv 第 2 行.*16 位",
        ),
        (
            "meter_id,boot_id,uptime_ms,word_high,word_low\n"
            "7,1,0,1\n",
            "readings.csv 第 2 行.*列数",
        ),
        (
            "meter_id,boot_id,uptime_ms,word_high\n"
            "7,1,0,1\n",
            "readings.csv 表头不匹配",
        ),
        (
            "meter_id,boot_id,uptime_ms,word_high,word_low\n"
            "7,-1,0,1,2\n",
            "readings.csv 第 2 行.*非负整数",
        ),
    ]
    for content, pattern in cases:
        with pytest.raises(CorrectionError) as excinfo:
            _job(content, CAL)
        assert __import__("re").search(pattern, str(excinfo.value))


def test_calibration_bad_rows_report_its_own_filename():
    # 校时日志坏行必须报校时文件名，而不是读数文件名
    bad_cal = "boot_id,uptime_ms,utc_ms\n1,0,1000\n1,0,2000\n"
    with pytest.raises(CorrectionError) as excinfo:
        _job(
            "meter_id,boot_id,uptime_ms,word_high,word_low\n7,1,0,0,1\n",
            bad_cal,
        )
    msg = str(excinfo.value)
    assert "calibration.csv" in msg and "readings.csv" not in msg
    assert "第 3 行" in msg


def test_reading_limit_enforced_without_buffering(monkeypatch):
    monkeypatch.setattr(corr, "MAX_READING_ROWS", 100)
    readings = (
        "meter_id,boot_id,uptime_ms,word_high,word_low\n"
        + "".join(f"7,1,{i},0,{i % 60000}\n" for i in range(101))
    )
    with pytest.raises(CorrectionError, match="readings.csv 第 102 行.*上限"):
        _job(readings, CAL)


def test_temp_files_cleaned_up_after_stream():
    job = _job(
        "meter_id,boot_id,uptime_ms,word_high,word_low\n7,1,0,0,1\n",
        CAL,
    )
    work_dir = job._work_dir  # noqa: SLF001
    list(job.stream())
    job.close()
    import os

    assert not os.path.exists(work_dir)


def test_header_only_reading_yields_header_only_output():
    rows = _run(
        "meter_id,boot_id,uptime_ms,word_high,word_low\n",
        CAL,
    )
    assert rows == [list(corr.OUTPUT_HEADERS)]


def test_empty_reading_file_is_a_bad_row():
    with pytest.raises(CorrectionError, match="readings.csv 为空或缺少表头"):
        _job("", CAL)


def test_empty_calibration_file_is_a_bad_row():
    with pytest.raises(CorrectionError, match="calibration.csv 为空或缺少表头"):
        _job(
            "meter_id,boot_id,uptime_ms,word_high,word_low\n7,1,0,0,1\n",
            "",
        )


# --------------------------------------------------------------- HTTP 层


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_correct_endpoint_returns_csv_with_bom():
    readings = (
        "meter_id,boot_id,uptime_ms,word_high,word_low\n"
        "7,1,0,0,100\n"
        "7,2,0,0,200\n"
    )
    async with await _client() as client:
        resp = await client.post(
            "/correct",
            files={
                "readings": ("readings.csv", readings.encode(), "text/csv"),
                "calibration": ("calibration.csv", CAL.encode(), "text/csv"),
            },
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.content.startswith(b"\xef\xbb\xbf")  # utf-8-sig BOM
    text = resp.content.decode("utf-8-sig")
    rows = list(__import__("csv").reader(io.StringIO(text)))
    assert rows[0] == list(corr.OUTPUT_HEADERS)
    # 跨启动撞秒，两行都待核对且保留校正秒
    assert all(r[4] == STATUS_REVIEW for r in rows[1:])
    assert {r[3] for r in rows[1:]} == {"1700000000"}


async def test_correct_endpoint_bad_row_returns_422_no_body_csv():
    bad = (
        "meter_id,boot_id,uptime_ms,word_high,word_low\n"
        "7,1,0,99999,1\n"
    )
    async with await _client() as client:
        resp = await client.post(
            "/correct",
            files={
                "readings": ("r.csv", bad.encode(), "text/csv"),
                "calibration": ("c.csv", CAL.encode(), "text/csv"),
            },
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "r.csv" in detail and "第 2 行" in detail
    assert "text/csv" not in resp.headers.get("content-type", "")
