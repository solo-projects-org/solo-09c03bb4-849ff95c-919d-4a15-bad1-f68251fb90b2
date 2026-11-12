"""校时核心逻辑单元测试（仅依赖标准库，不需要 FastAPI / 网络）。"""

from __future__ import annotations

import csv
import os
import shutil

import pytest

from app.correction import (
    CAL_FIELDS,
    OUTPUT_FIELDS,
    READING_FIELDS,
    CalPoint,
    CorrectionError,
    decode_count,
    interpolate_second,
    parse_calibration,
    run_correction,
)


def _csv_bytes(header: tuple[str, ...], rows: list[tuple]) -> bytes:
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


CAL_ROWS = [
    (1, 1000, 1_700_000_001_000),
    (1, 61000, 1_700_000_060_000),
    (1, 121000, 1_700_000_120_000),
    (2, 500, 1_700_000_180_500),
    (2, 60500, 1_700_000_240_500),
]


@pytest.fixture
def calibration() -> bytes:
    return _csv_bytes(CAL_FIELDS, [list(r) for r in CAL_ROWS])


# ---- 校时日志解析 ----------------------------------------------------------


def test_parse_calibration_groups_by_boot_and_sorts():
    # 故意打乱行序，解析后仍按开机毫秒升序
    data = _csv_bytes(
        CAL_FIELDS,
        [
            (1, 61000, 1_700_000_060_000),
            (2, 500, 1_700_000_180_500),
            (1, 1000, 1_700_000_001_000),
        ],
    )
    boots = parse_calibration(data, "cal.csv")
    assert set(boots) == {1, 2}
    assert [p.boot_ms for p in boots[1]] == [1000, 61000]
    assert boots[2] == [CalPoint(500, 1_700_000_180_500)]


def test_parse_calibration_rejects_non_increasing_boot_ms():
    data = _csv_bytes(
        CAL_FIELDS,
        [
            (1, 1000, 1_700_000_001_000),
            (1, 1000, 1_700_000_002_000),  # 开机毫秒相等
        ],
    )
    with pytest.raises(CorrectionError, match="开机毫秒不严格递增"):
        parse_calibration(data, "cal.csv")


def test_parse_calibration_rejects_non_increasing_utc_ms():
    data = _csv_bytes(
        CAL_FIELDS,
        [
            (1, 1000, 1_700_000_005_000),
            (1, 2000, 1_700_000_005_000),  # UTC 毫秒相等
        ],
    )
    with pytest.raises(CorrectionError, match="UTC 毫秒不严格递增"):
        parse_calibration(data, "cal.csv")


def test_parse_calibration_rejects_bad_header():
    data = b"boot_id,boot_ms\n1,2\n"
    with pytest.raises(CorrectionError, match="第 1 行"):
        parse_calibration(data, "cal.csv")


def test_parse_calibration_bad_row_reports_filename_and_line():
    data = _csv_bytes(CAL_FIELDS, [(1, "x", 100)])
    with pytest.raises(CorrectionError, match="cal.csv 第 2 行"):
        parse_calibration(data, "cal.csv")


def test_parse_calibration_enforces_1000_row_limit():
    rows = [(1, i * 1000 + 1, 1_700_000_000_000 + i * 1000) for i in range(1001)]
    data = _csv_bytes(CAL_FIELDS, rows)
    with pytest.raises(CorrectionError, match="1000 行上限"):
        parse_calibration(data, "cal.csv")


def test_parse_calibration_rejects_negative_values():
    data = _csv_bytes(CAL_FIELDS, [(1, -5, 100)])
    with pytest.raises(CorrectionError, match="boot_ms 不能为负"):
        parse_calibration(data, "cal.csv")


# ---- 线性换算 ---------------------------------------------------------------


def test_interpolate_at_endpoints_is_exact():
    points = [CalPoint(1000, 1_700_000_001_000), CalPoint(61000, 1_700_000_060_000)]
    assert interpolate_second(points, 1000) == 1_700_000_001
    assert interpolate_second(points, 61000) == 1_700_000_060


def test_interpolate_midpoint_floors_to_second():
    # boot 1：m=31000 -> utc_ms = 1700000001000 + 59000*30000//60000
    # = 1700000001000 + 29500 = 1700000030500 -> 秒 1700000030
    points = [
        CalPoint(1000, 1_700_000_001_000),
        CalPoint(61000, 1_700_000_060_000),
    ]
    assert interpolate_second(points, 31000) == 1_700_000_030


def test_interpolate_floor_division_no_floating_point():
    # utc_ms 增量除不尽，验证整数向下取整（无浮点误差），再向下取整秒
    points = [CalPoint(0, 1_500), CalPoint(3, 4_600)]
    # m=1：1500 + 3100*1//3 = 2533 ms -> 秒 2
    # m=2：1500 + 3100*2//3 = 3566 ms -> 秒 3
    assert interpolate_second(points, 1) == 2
    assert interpolate_second(points, 2) == 3


def test_interpolate_outside_range_returns_none():
    points = [CalPoint(1000, 1_700_000_001_000), CalPoint(61000, 1_700_000_060_000)]
    assert interpolate_second(points, 999) is None
    assert interpolate_second(points, 61001) is None
    assert interpolate_second([], 100) is None


def test_decode_count_high_word_first():
    assert decode_count(0x0001, 0x86A0) == 100000
    assert decode_count(0xFFFF, 0xFFFF) == 0xFFFFFFFF
    assert decode_count(0, 100) == 100


# ---- 端到端主流程 -----------------------------------------------------------


@pytest.fixture
def work_dir(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    yield str(path)
    shutil.rmtree(path, ignore_errors=True)


def _run(calibration, rows, work_dir, cal_name="cal.csv", rd_name="readings.csv"):
    data = _csv_bytes(READING_FIELDS, rows)
    output = run_correction(
        calibration, data, cal_name, rd_name, work_dir=work_dir
    )
    with open(output, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == list(OUTPUT_FIELDS)
        return list(reader)


def test_end_to_end_collision_marks_both_rows_and_keeps_original_order(
    calibration, work_dir
):
    rows = [
        (1001, 1, 1000, 0, 100),   # 物理行 2 -> 秒 1700000001，正常
        (1001, 2, 10500, 0, 102),  # 物理行 3 -> 秒 1700000190
        (1001, 1, 31000, 0, 101),  # 物理行 4 -> 秒 1700000030，正常
        (1001, 2, 10500, 0, 5),    # 物理行 5 -> 秒 1700000190，与行 3 撞秒
    ]
    result = _run(calibration, rows, work_dir)
    by_line = {r["source_row"]: r for r in result}
    for line in ("3", "5"):
        assert by_line[line]["status"] == "待核对"
        assert "同表同秒" in by_line[line]["review_reason"]
    assert by_line["2"]["status"] == "已校正"
    assert by_line["4"]["status"] == "已校正"
    # 按读数原序输出（乱序输入也保持物理行序）
    assert [r["source_row"] for r in result] == ["2", "3", "4", "5"]


def test_cross_boot_collision_uses_overlapping_utc_windows(work_dir):
    # 两个启动的 UTC 覆盖区间有重叠，制造真正的跨启动同表同秒
    cal = _csv_bytes(
        CAL_FIELDS,
        [
            (1, 0, 1_700_000_000_000),
            (1, 10_000, 1_700_000_010_000),
            (2, 0, 1_700_000_005_000),
            (2, 10_000, 1_700_000_015_000),
        ],
    )
    rows = [
        (900, 1, 5000, 0, 1),   # utc 1700000005000 -> 秒 ...005
        (900, 2, 0, 0, 2),      # utc 1700000005000 -> 秒 ...005（跨启动撞秒）
        (901, 1, 5000, 0, 3),   # 不同表，不撞
    ]
    result = _run(cal, rows, work_dir)
    by_meter = {(r["source_row"]): r for r in result}
    assert by_meter["2"]["status"] == "待核对"
    assert by_meter["3"]["status"] == "待核对"
    assert by_meter["4"]["status"] == "已校正"


def test_unknown_boot_and_out_of_range_are_pending(calibration, work_dir):
    rows = [
        (1005, 3, 1000, 0, 7),     # 未知启动
        (1001, 1, 500, 0, 99),     # 早于首个校时点
        (1001, 1, 121500, 0, 103),  # 晚于末个校时点
    ]
    result = _run(calibration, rows, work_dir)
    assert result[0]["status"] == "待核对"
    assert "无校时数据" in result[0]["review_reason"]
    assert result[0]["corrected_second"] == ""
    for r in result[1:]:
        assert r["status"] == "待核对"
        assert "覆盖区间" in r["review_reason"]
        assert r["corrected_second"] == ""


def test_different_meters_same_second_are_both_ok(calibration, work_dir):
    rows = [
        (1001, 1, 31000, 0, 1),
        (1002, 1, 31000, 0, 2),
    ]
    result = _run(calibration, rows, work_dir)
    assert all(r["status"] == "已校正" for r in result)
    assert result[0]["corrected_second"] == result[1]["corrected_second"]


def test_total_count_decoded_high_word_first(calibration, work_dir):
    rows = [(1001, 1, 1000, 0x0001, 0x86A0)]
    result = _run(calibration, rows, work_dir)
    assert result[0]["total_count"] == "100000"


def test_bad_reading_row_reports_filename_and_line_and_no_output(
    calibration, work_dir
):
    data = _csv_bytes(
        READING_FIELDS,
        [
            (1001, 1, 1000, 0, 1),
            (1001, 1, 2000, 70000, 1),  # word_high 超 16 位
        ],
    )
    before = set(os.listdir(work_dir))
    with pytest.raises(CorrectionError, match=r"readings\.csv 第 3 行"):
        run_correction(calibration, data, "cal.csv", "readings.csv", work_dir=work_dir)
    # 失败后临时目录被清理，不留半成品
    assert set(os.listdir(work_dir)) == before


def test_bad_column_count_reports_line(calibration, work_dir):
    # 表头 5 列，数据只有 3 列
    data = _csv_bytes(READING_FIELDS, [(1, 2, 3)])
    with pytest.raises(CorrectionError, match="第 2 行"):
        run_correction(calibration, data, "cal.csv", "readings.csv", work_dir=work_dir)


def test_word_out_of_uint16_range_rejected(calibration, work_dir):
    data = _csv_bytes(READING_FIELDS, [(1, 1, 1000, -1, 0)])
    with pytest.raises(CorrectionError, match="word_high"):
        run_correction(calibration, data, "cal.csv", "readings.csv", work_dir=work_dir)


def test_empty_calibration_marks_everything_pending(work_dir):
    cal = _csv_bytes(CAL_FIELDS, [])
    rows = [(1, 1, 100, 0, 1), (2, 1, 200, 0, 2)]
    result = _run(cal, rows, work_dir)
    assert all(r["status"] == "待核对" for r in result)
    assert all("无校时数据" in r["review_reason"] for r in result)


def test_duplicate_second_all_rows_marked_even_first_one(calibration, work_dir):
    rows = [
        (1001, 1, 31000, 0, 1),
        (1001, 1, 31000, 0, 2),  # 完全同秒
        (1001, 1, 31000, 0, 3),
    ]
    result = _run(calibration, rows, work_dir)
    assert all(r["status"] == "待核对" for r in result)
    assert len({r["corrected_second"] for r in result}) == 1


def test_empty_readings_file_returns_header_only(calibration, work_dir):
    data = (",".join(READING_FIELDS) + "\n").encode()
    output = run_correction(
        calibration, data, "cal.csv", "readings.csv", work_dir=work_dir
    )
    with open(output, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows == [list(OUTPUT_FIELDS)]


def test_utf8_bom_header_is_accepted(work_dir):
    cal = ("﻿" + ",".join(CAL_FIELDS) + "\n1,1000,1700000001000\n").encode()
    data = (
        "﻿" + ",".join(READING_FIELDS) + "\n7,1,1000,0,5\n"
    ).encode()
    output = run_correction(
        cal, data, "cal.csv", "readings.csv", work_dir=work_dir
    )
    with open(output, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["meter_id"] == "7"
    assert rows[0]["status"] == "已校正"


def test_source_row_is_physical_line_number_with_gaps_and_blank_lines(
    calibration, work_dir
):
    # 手工构造含空行的文件：物理行号会跳号
    text = ",".join(READING_FIELDS) + "\n"
    text += "1001,1,1000,0,1\n"   # 物理行 2
    text += "\n"                  # 物理行 3 空行
    text += "1001,1,2000,0,2\n"   # 物理行 4
    output = run_correction(
        calibration, text.encode(), "cal.csv", "readings.csv", work_dir=work_dir
    )
    with open(output, encoding="utf-8", newline="") as fh:
        rows_out = list(csv.DictReader(fh))
    assert [r["source_row"] for r in rows_out] == ["2", "4"]
