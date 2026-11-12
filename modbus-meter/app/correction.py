"""缓存电表读数校时。

网关断网期间本地时钟走偏，厂务侧需要依据同网关的校时日志（启动编号、
开机毫秒、UTC 毫秒）把缓存读数的开机毫秒换算回 UTC 整秒，再送入每表每秒
只收一笔的能耗库。

约束与规则（见 README）：

- 校时日志最多 1000 行；同一次启动（``boot_id``）的校时点按开机毫秒排序，
  开机毫秒与 UTC 毫秒都必须**严格递增**，否则视为坏文件；
- 只在同次启动的**相邻校时点之间（含端点）**做线性换算，结果向下取整到
  UTC 整秒；没有校时数据的启动、落在首个校时点之前或末个校时点之后的
  读数无法换算，标记为待核对；
- 校正后同表同秒的多笔读数（含跨启动）全部待核对；
- 最多处理 100 万条乱序读数，读数与撞秒索引落盘到临时 SQLite 文件，
  不在内存中攒下全部读数、撞秒索引或输出；
- 任何坏行都抛出 :class:`CorrectionError`（含文件名与行号），不产出部分结果。

换算采用整数算术，避免浮点误差：设相邻校时点
``(m0, u0) -> (m1, u1)``，读数开机毫秒为 ``m``（``m0 <= m <= m1``），则

    utc_ms = u0 + (u1 - u0) * (m - m0) // (m1 - m0)        # 向下取整
    校正秒 = utc_ms // 1000
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import tempfile
from bisect import bisect_left
from dataclasses import dataclass
from typing import IO

# ---- 列定义（顺序即 CSV 表头顺序）-----------------------------------------

CAL_FIELDS = ("boot_id", "boot_ms", "utc_ms")
READING_FIELDS = ("meter_id", "boot_id", "boot_ms", "word_high", "word_low")
OUTPUT_FIELDS = (
    "source_row",
    "meter_id",
    "total_count",
    "corrected_second",
    "status",
    "review_reason",
)

MAX_CALIBRATION_ROWS = 1000
MAX_READING_ROWS = 1_000_000
INSERT_BATCH = 5000
# SQLite 页缓存上限（cache_size 以 KiB 计），配合容器 192 MiB 限制保守取值
SQLITE_CACHE_KIB = 8 * 1024

STATUS_OK = "已校正"
STATUS_REVIEW = "待核对"

REASON_NO_CALIBRATION = "该启动无校时数据，无法换算"
REASON_OUT_OF_RANGE = "开机毫秒超出校时点覆盖区间，无法换算"
REASON_DUPLICATE_SECOND = "校正后同表同秒存在多笔读数，待核对"


class CorrectionError(ValueError):
    """输入 CSV 存在坏行或不满足约束；消息含文件名与行号。"""


@dataclass(frozen=True)
class CalPoint:
    """单次启动内的一个校时点。"""

    boot_ms: int
    utc_ms: int


# ---- 解析工具 ---------------------------------------------------------------


def _parse_int(raw: str, filename: str, line_no: int, column: str) -> int:
    text = raw.strip()
    if text == "":
        raise CorrectionError(f"{filename} 第 {line_no} 行：列 {column} 不能为空")
    try:
        return int(text, 10)
    except ValueError as exc:
        raise CorrectionError(
            f"{filename} 第 {line_no} 行：列 {column} 不是整数（{raw!r}）"
        ) from exc


def _check_header(
    reader: csv.reader, expected: tuple[str, ...], filename: str
) -> None:
    try:
        header = next(reader)
    except StopIteration:
        raise CorrectionError(f"{filename} 第 1 行：缺少表头")
    if [c.strip() for c in header] != list(expected):
        raise CorrectionError(
            f"{filename} 第 1 行：表头应为 {','.join(expected)}，"
            f"实际为 {','.join(header)}"
        )


def _decode_text(data: bytes, filename: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CorrectionError(
            f"{filename}：不是合法的 UTF-8 文本（{exc}）"
        ) from exc


def _read_bytes(source: "bytes | IO[bytes]") -> bytes:
    """统一把 bytes 或二进制流读成 bytes（流由调用方决定是否已落盘）。"""
    if isinstance(source, bytes):
        return source
    data = source.read()
    if isinstance(data, str):  # 兜底：文本流
        data = data.encode("utf-8")
    return data


def _as_binary(source: "bytes | IO[bytes]") -> IO[bytes]:
    """统一包装成可逐行读取的二进制文件对象（bytes 包一层 BytesIO）。"""
    if isinstance(source, bytes):
        return io.BytesIO(source)
    return source


# ---- 校时日志 ---------------------------------------------------------------


def parse_calibration(
    source: "bytes | IO[bytes]", filename: str
) -> dict[int, list[CalPoint]]:
    """解析校时日志，返回 ``boot_id -> 按开机毫秒升序的校时点``。

    ``source`` 可以是 ``bytes`` 或二进制文件对象（后者按块读取，
    小文件可直接落内存，大文件由调用方假脱机到磁盘）。

    :raises CorrectionError: 表头不符、行数超限、字段非法或同启动内
        开机毫秒 / UTC 毫秒不严格递增。
    """
    boots: dict[int, list[CalPoint]] = {}
    text = _decode_text(_read_bytes(source), filename)
    reader = csv.reader(io.StringIO(text, newline=""))
    _check_header(reader, CAL_FIELDS, filename)
    count = 0
    for line_no, row in enumerate(reader, start=2):
        if not row or all(cell.strip() == "" for cell in row):
            continue  # 容忍空行
        count += 1
        if count > MAX_CALIBRATION_ROWS:
            raise CorrectionError(
                f"{filename}：校时日志超过 {MAX_CALIBRATION_ROWS} 行上限"
            )
        if len(row) != len(CAL_FIELDS):
            raise CorrectionError(
                f"{filename} 第 {line_no} 行：应有 {len(CAL_FIELDS)} 列，"
                f"实际 {len(row)} 列"
            )
        boot_id = _parse_int(row[0], filename, line_no, "boot_id")
        boot_ms = _parse_int(row[1], filename, line_no, "boot_ms")
        utc_ms = _parse_int(row[2], filename, line_no, "utc_ms")
        if boot_ms < 0:
            raise CorrectionError(f"{filename} 第 {line_no} 行：boot_ms 不能为负")
        if utc_ms < 0:
            raise CorrectionError(f"{filename} 第 {line_no} 行：utc_ms 不能为负")
        boots.setdefault(boot_id, []).append(
            CalPoint(boot_ms=boot_ms, utc_ms=utc_ms)
        )

    for boot_id, points in boots.items():
        points.sort(key=lambda p: p.boot_ms)
        for prev, cur in zip(points, points[1:]):
            if cur.boot_ms <= prev.boot_ms:
                raise CorrectionError(
                    f"{filename}：启动 {boot_id} 的开机毫秒不严格递增"
                    f"（{prev.boot_ms} 后出现 {cur.boot_ms}）"
                )
            if cur.utc_ms <= prev.utc_ms:
                raise CorrectionError(
                    f"{filename}：启动 {boot_id} 的 UTC 毫秒不严格递增"
                    f"（{prev.utc_ms} 后出现 {cur.utc_ms}）"
                )
    return boots


# ---- 线性换算 ---------------------------------------------------------------


def interpolate_second(points: list[CalPoint], boot_ms: int) -> int | None:
    """在相邻校时点（含端点）之间线性换算并向下取 UTC 整秒。

    无法换算（落在区间外）时返回 ``None``。``points`` 已按开机毫秒升序。
    """
    if not points:
        return None
    keys = [p.boot_ms for p in points]
    idx = bisect_left(keys, boot_ms)
    if idx < len(points) and points[idx].boot_ms == boot_ms:
        # 恰好落在校时点上（含端点）
        return points[idx].utc_ms // 1000
    if idx == 0 or idx == len(points):
        return None  # 早于首个点或晚于末个点
    left = points[idx - 1]
    right = points[idx]
    span_ms = right.boot_ms - left.boot_ms
    utc_ms = left.utc_ms + (
        (right.utc_ms - left.utc_ms) * (boot_ms - left.boot_ms)
    ) // span_ms
    return utc_ms // 1000


def decode_count(word_high: int, word_low: int) -> int:
    """两个 16 位无符号寄存器字按高字在前（big-endian）拼成累计计数。"""
    return ((word_high & 0xFFFF) << 16) | (word_low & 0xFFFF)


# ---- 主流程：流式解析 + 落盘 + 撞秒标记 + 流式输出 -------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    # 内存尽量克制：小页缓存、不建回滚日志、临时对象也落盘
    conn.execute(f"PRAGMA cache_size = -{SQLITE_CACHE_KIB}")
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute(
        """
        CREATE TABLE readings (
            seq              INTEGER PRIMARY KEY,
            meter_id         INTEGER NOT NULL,
            total_count      INTEGER NOT NULL,
            corrected_second INTEGER,
            status           TEXT NOT NULL,
            review_reason    TEXT NOT NULL
        )
        """
    )
    return conn


def run_correction(
    calibration: "bytes | IO[bytes]",
    readings: "bytes | IO[bytes]",
    calibration_filename: str,
    readings_filename: str,
    work_dir: str | None = None,
) -> str:
    """校时主流程，返回结果 CSV 的临时文件路径。

    ``calibration`` / ``readings`` 可以是 ``bytes`` 或二进制文件对象；
    大文件请由调用方假脱机（spool）到磁盘后传入，避免整份驻留内存。
    调用方负责在响应结束后清理返回文件所在的临时目录。

    全程流式处理：读数逐行解析后批量写入磁盘 SQLite，撞秒通过磁盘索引
    检测，结果按原序逐行导出，内存中不保留全量读数、撞秒索引或输出。

    :raises CorrectionError: 任一文件存在坏行或超限时抛出，此时临时目录
        已整体清理，不会留下任何半成品结果。
    """
    boots = parse_calibration(calibration, calibration_filename)

    tmp_dir = tempfile.mkdtemp(prefix="correction-", dir=work_dir)
    db_path = os.path.join(tmp_dir, "readings.db")
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(db_path)
        pending: list[tuple] = []

        def flush() -> None:
            if pending:
                conn.executemany(
                    "INSERT INTO readings (seq, meter_id, total_count, "
                    "corrected_second, status, review_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    pending,
                )
                conn.commit()
                pending.clear()

        # 读数按行流式解码（utf-8-sig 去 BOM），不构造整份字符串
        binary = _as_binary(readings)
        try:
            text_stream = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            reader = csv.reader(text_stream)
            _check_header(reader, READING_FIELDS, readings_filename)
            row_count = 0
            for line_no, row in enumerate(reader, start=2):
                if not row or all(cell.strip() == "" for cell in row):
                    continue
                row_count += 1
                if row_count > MAX_READING_ROWS:
                    raise CorrectionError(
                        f"{readings_filename}：读数超过 "
                        f"{MAX_READING_ROWS} 行上限"
                    )
                if len(row) != len(READING_FIELDS):
                    raise CorrectionError(
                        f"{readings_filename} 第 {line_no} 行："
                        f"应有 {len(READING_FIELDS)} 列，实际 {len(row)} 列"
                    )
                meter_id = _parse_int(
                    row[0], readings_filename, line_no, "meter_id"
                )
                boot_id = _parse_int(
                    row[1], readings_filename, line_no, "boot_id"
                )
                boot_ms = _parse_int(
                    row[2], readings_filename, line_no, "boot_ms"
                )
                word_high = _parse_int(
                    row[3], readings_filename, line_no, "word_high"
                )
                word_low = _parse_int(
                    row[4], readings_filename, line_no, "word_low"
                )
                if meter_id < 0:
                    raise CorrectionError(
                        f"{readings_filename} 第 {line_no} 行：meter_id 不能为负"
                    )
                if boot_ms < 0:
                    raise CorrectionError(
                        f"{readings_filename} 第 {line_no} 行：boot_ms 不能为负"
                    )
                if not 0 <= word_high <= 0xFFFF:
                    raise CorrectionError(
                        f"{readings_filename} 第 {line_no} 行：word_high 超出 "
                        "16 位无符号范围（0..65535）"
                    )
                if not 0 <= word_low <= 0xFFFF:
                    raise CorrectionError(
                        f"{readings_filename} 第 {line_no} 行：word_low 超出 "
                        "16 位无符号范围（0..65535）"
                    )

                points = boots.get(boot_id)
                if points is None:
                    corrected: int | None = None
                    status = STATUS_REVIEW
                    reason = REASON_NO_CALIBRATION
                else:
                    corrected = interpolate_second(points, boot_ms)
                    if corrected is None:
                        status = STATUS_REVIEW
                        reason = REASON_OUT_OF_RANGE
                    else:
                        status = STATUS_OK
                        reason = ""

                # source_row 即读数文件物理行号（表头为第 1 行），乱序不影响原序
                pending.append(
                    (
                        line_no,
                        meter_id,
                        decode_count(word_high, word_low),
                        corrected,
                        status,
                        reason,
                    )
                )
                if len(pending) >= INSERT_BATCH:
                    flush()
        except UnicodeDecodeError as exc:
            raise CorrectionError(
                f"{readings_filename}：不是合法的 UTF-8 文本（{exc}）"
            ) from exc
        flush()

        # 撞秒检测：同表同校正秒出现多笔即全部待核对（跨启动同样适用）。
        # 无法换算的行 corrected_second 为 NULL，不参与撞秒。
        conn.execute(
            "CREATE INDEX idx_meter_second ON readings (meter_id, corrected_second)"
        )
        conn.execute(
            """
            UPDATE readings
               SET status = ?, review_reason = ?
             WHERE corrected_second IS NOT NULL
               AND (meter_id, corrected_second) IN (
                       SELECT meter_id, corrected_second
                         FROM readings
                        WHERE corrected_second IS NOT NULL
                        GROUP BY meter_id, corrected_second
                       HAVING COUNT(*) > 1
                   )
            """,
            (STATUS_REVIEW, REASON_DUPLICATE_SECOND),
        )
        conn.commit()

        output_path = os.path.join(tmp_dir, "corrected.csv")
        # 流式导出：分批拉取逐行写文件，不把结果集载入内存
        with open(output_path, "w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(OUTPUT_FIELDS)
            cursor = conn.execute(
                "SELECT seq, meter_id, total_count, corrected_second, "
                "status, review_reason FROM readings ORDER BY seq"
            )
            while True:
                batch = cursor.fetchmany(INSERT_BATCH)
                if not batch:
                    break
                for seq, meter_id, total_count, second, status, reason in batch:
                    writer.writerow(
                        [
                            seq,
                            meter_id,
                            total_count,
                            "" if second is None else second,
                            status,
                            reason,
                        ]
                    )
        return output_path
    except Exception:
        # 任何失败都清掉临时库与半成品，绝不返回部分结果
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        if conn is not None:
            conn.close()
