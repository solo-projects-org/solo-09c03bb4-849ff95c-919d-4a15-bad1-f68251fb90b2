"""网关缓存电表读数校时。

输入同一网关的两份 CSV：

- 读数 CSV：表号、启动编号、开机毫秒、两个 16 位无符号寄存器字
  （高字在前，按 ``/read`` 同样的方式拼成 32 位无符号累计计数）；
- 校时日志 CSV：最多 1000 行，含启动编号、开机毫秒、UTC 毫秒，
  同一次启动内按开机毫秒排序，且开机毫秒与 UTC 毫秒都严格递增。

只在同次启动的**相邻**校时点之间（含端点）做线性换算，UTC 毫秒向下取整秒；
无法换算的读数标为“待核对”；校正后同表同秒（启动编号不同也算）的多笔读数
全部标为“待核对”。

设计约束（最多 100 万条乱序读数，API 容器限 192 MiB）：

- 上传先落盘，逐行流式解析，绝不在内存攒下全部读数；
- 撞秒索引与结果排序交给**磁盘上的 SQLite**（页缓存限 8 MiB、临时对象落盘、
  关闭 mmap），不维护任何按 (表号, 秒) 的内存索引，也不缓存输出；
- 全量校验通过后才开始产出结果；任何坏行直接报错（含文件名与行号），
  不发部分结果。
"""

from __future__ import annotations

import bisect
import csv
import io
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass

# ---------------------------------------------------------------- 常量

MAX_READING_ROWS = 1_000_000
MAX_CALIBRATION_ROWS = 1_000

READING_HEADERS = ("meter_id", "boot_id", "uptime_ms", "word_high", "word_low")
CALIBRATION_HEADERS = ("boot_id", "uptime_ms", "utc_ms")

# 输出列（中文表头；响应带 utf-8-sig BOM，Excel 可直接打开）
OUTPUT_HEADERS = ("源行号", "表号", "累计计数", "校正秒", "状态", "待核对原因")

STATUS_OK = "校正"
STATUS_REVIEW = "待核对"

REASON_OUT_OF_RANGE = "开机毫秒不在该启动校时点覆盖范围内，无法线性换算"
REASON_NO_CALIBRATION = "该启动编号无校时日志，无法换算"
REASON_COLLISION = "校正后与同表同秒的其他读数撞秒（可能来自不同启动）"

SQLITE_CACHE_KIB = 8 * 1024  # 页缓存上限 8 MiB
INSERT_BATCH = 2_000
OUTPUT_FLUSH_BYTES = 128 * 1024
UTF8_BOM = b"\xef\xbb\xbf"


class CorrectionError(ValueError):
    """输入 CSV 校验失败；消息含文件名与行号，不产出任何部分结果。"""


# ---------------------------------------------------------------- 解析辅助


def _parse_nonneg_int(raw: str) -> int:
    """严格解析非负整数：不允许空白、符号、小数或其它字符。"""
    s = raw.strip()
    if not s or not s.isdigit():
        raise ValueError("需要非负整数")
    return int(s)


def _parse_word(raw: str) -> int:
    value = _parse_nonneg_int(raw)
    if value > 0xFFFF:
        raise ValueError("寄存器字必须是 0..65535 的 16 位无符号整数")
    return value


def _bad_row(filename: str, line_no: int, detail: str) -> CorrectionError:
    return CorrectionError(f"文件 {filename} 第 {line_no} 行：{detail}")


def _header_index(
    header: list[str] | None, expected: tuple[str, ...], filename: str
) -> dict[str, int]:
    if header is None:
        raise CorrectionError(f"文件 {filename} 为空或缺少表头")
    stripped = [h.strip() for h in header]
    if any(h == "" for h in stripped):
        raise CorrectionError(f"文件 {filename} 表头存在空列名")
    if len(set(stripped)) != len(stripped):
        raise CorrectionError(f"文件 {filename} 表头存在重复列名：{stripped}")
    missing = [h for h in expected if h not in stripped]
    extra = [h for h in stripped if h not in expected]
    if missing or extra:
        problems = []
        if missing:
            problems.append(f"缺少列 {missing}")
        if extra:
            problems.append(f"存在多余列 {extra}")
        raise CorrectionError(
            f"文件 {filename} 表头不匹配（期望 {list(expected)}）："
            + "；".join(problems)
        )
    return {name: stripped.index(name) for name in expected}


# ---------------------------------------------------------------- 校时点


@dataclass(frozen=True)
class CalPoint:
    uptime_ms: int
    utc_ms: int


class CalibrationTable:
    """按启动编号组织的校时点表；每次启动内开机毫秒严格递增。"""

    def __init__(self, points_by_boot: dict[int, list[CalPoint]]) -> None:
        self._boots = points_by_boot
        # 预计算每个启动的开机毫秒数组，供二分查找复用（避免每行重复分配）
        self._uptimes = {
            boot: [p.uptime_ms for p in pts]
            for boot, pts in points_by_boot.items()
        }

    def has_boot(self, boot_id: int) -> bool:
        return boot_id in self._boots

    def correct_second(self, boot_id: int, uptime_ms: int) -> int | None:
        """线性换算并向下取 UTC 整秒；无法换算返回 None。

        只在同次启动的相邻校时点之间（含端点）插值，区间外一律不换算。
        """
        pts = self._boots.get(boot_id)
        if not pts:
            return None
        idx = bisect.bisect_right(self._uptimes[boot_id], uptime_ms) - 1
        if idx < 0:
            return None  # 早于该启动第一个校时点
        if idx == len(pts) - 1:
            if uptime_ms != pts[idx].uptime_ms:
                return None  # 晚于最后一个校时点，不外推
            return pts[idx].utc_ms // 1000
        p0, p1 = pts[idx], pts[idx + 1]
        # 纯整数线性插值，避免浮点漂移；uptime_ms 位于 [p0, p1)
        utc_ms = p0.utc_ms + (
            (p1.utc_ms - p0.utc_ms) * (uptime_ms - p0.uptime_ms)
            // (p1.uptime_ms - p0.uptime_ms)
        )
        return utc_ms // 1000


def load_calibration(path: str, filename: str) -> CalibrationTable:
    """读取并校验校时日志（最多 1000 行）。"""
    fh = open(path, "r", encoding="utf-8-sig", newline="")
    try:
        reader = csv.reader(fh)
        try:
            header = next(reader, None)
        except UnicodeDecodeError as exc:
            raise CorrectionError(f"文件 {filename} 编码不是 UTF-8：{exc}") from exc
        idx = _header_index(header, CALIBRATION_HEADERS, filename)
        ordered: dict[int, list[CalPoint]] = {}
        count = 0
        try:
            for raw in reader:
                line_no = reader.line_num
                if not raw or all(cell.strip() == "" for cell in raw):
                    raise _bad_row(filename, line_no, "存在空行")
                if len(raw) != len(idx):
                    raise _bad_row(
                        filename, line_no, f"列数应为 {len(idx)}，实际 {len(raw)}"
                    )
                try:
                    boot_id = _parse_nonneg_int(raw[idx["boot_id"]])
                    uptime_ms = _parse_nonneg_int(raw[idx["uptime_ms"]])
                    utc_ms = _parse_nonneg_int(raw[idx["utc_ms"]])
                except ValueError as exc:
                    raise _bad_row(filename, line_no, str(exc)) from exc
                pts = ordered.setdefault(boot_id, [])
                if pts:
                    prev = pts[-1]
                    if uptime_ms <= prev.uptime_ms:
                        raise _bad_row(
                            filename,
                            line_no,
                            f"启动 {boot_id} 的开机毫秒未严格递增"
                            f"（{prev.uptime_ms} -> {uptime_ms}）",
                        )
                    if utc_ms <= prev.utc_ms:
                        raise _bad_row(
                            filename,
                            line_no,
                            f"启动 {boot_id} 的 UTC 毫秒未严格递增"
                            f"（{prev.utc_ms} -> {utc_ms}）",
                        )
                pts.append(CalPoint(uptime_ms=uptime_ms, utc_ms=utc_ms))
                count += 1
                if count > MAX_CALIBRATION_ROWS:
                    raise _bad_row(
                        filename, line_no,
                        f"校时日志超过 {MAX_CALIBRATION_ROWS} 行上限",
                    )
        except csv.Error as exc:
            raise _bad_row(filename, reader.line_num, f"CSV 解析失败：{exc}") from exc
        except UnicodeDecodeError as exc:
            raise CorrectionError(f"文件 {filename} 编码不是 UTF-8：{exc}") from exc
    finally:
        fh.close()
    return CalibrationTable(ordered)


# ---------------------------------------------------------------- 落盘与 SQLite


def spool_upload(upload_file, tmp_dir: str) -> tuple[str, str]:
    """把上传内容流式写入临时目录，返回 (临时路径, 原始文件名)。"""
    filename = upload_file.filename or "upload.csv"
    fd, path = tempfile.mkstemp(prefix="spool-", suffix=".csv", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = upload_file.file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
    return path, filename


def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    # 限制内存占用：小页缓存、临时对象/排序落盘、关闭 mmap
    conn.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_KIB}")  # 负值单位 KiB
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA mmap_size=0")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE readings ("
        " seq INTEGER PRIMARY KEY,"          # 读数原序（1 起）
        " source_line INTEGER NOT NULL,"     # CSV 物理行号（表头为第 1 行）
        " meter_id INTEGER NOT NULL,"
        " cumulative INTEGER NOT NULL,"      # 高字在前拼出的 32 位无符号计数
        " corrected_second INTEGER,"         # NULL 表示无法换算
        " status TEXT NOT NULL,"
        " reason TEXT"
        ")"
    )
    return conn


# ---------------------------------------------------------------- 作业对象


class CorrectionJob:
    """一次校时请求的全部磁盘资源；close() 后不留临时文件。"""

    def __init__(self, work_dir: str, conn: sqlite3.Connection, total: int) -> None:
        self._work_dir = work_dir
        self._conn = conn
        self.total = total

    def stream(self) -> Iterator[bytes]:
        """按读数原序（seq 升序）流式产出结果 CSV，游标分批取行。"""
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(OUTPUT_HEADERS)
        cursor = self._conn.execute(
            "SELECT source_line, meter_id, cumulative, corrected_second,"
            " status, reason FROM readings ORDER BY seq"
        )
        for source_line, meter_id, cumulative, second, status, reason in cursor:
            writer.writerow([
                source_line,
                meter_id,
                cumulative,
                "" if second is None else second,
                status,
                reason or "",
            ])
            if buffer.tell() >= OUTPUT_FLUSH_BYTES:
                yield buffer.getvalue().encode("utf-8")
                buffer.seek(0)
                buffer.truncate(0)
        tail = buffer.getvalue()
        if tail:
            yield tail.encode("utf-8")

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            shutil.rmtree(self._work_dir, ignore_errors=True)


def prepare_correction(readings_upload, calibration_upload) -> CorrectionJob:
    """落盘两份上传并完成全部前置校验与撞秒标记。

    任何坏行抛 :class:`CorrectionError`（含文件名与行号），此时无任何输出。
    """
    work_dir = tempfile.mkdtemp(prefix="correction-")
    try:
        reading_path, reading_name = spool_upload(readings_upload, work_dir)
        calibration_path, calibration_name = spool_upload(calibration_upload, work_dir)

        # 校时日志很小（<=1000 行），先全量校验
        table = load_calibration(calibration_path, calibration_name)

        conn = _open_db(os.path.join(work_dir, "readings.sqlite"))
        try:
            total = _ingest_readings(reading_path, reading_name, table, conn)
        except Exception:
            conn.close()
            raise
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    return CorrectionJob(
        work_dir=work_dir,
        conn=conn,
        total=total,
    )


def _ingest_readings(
    reading_path: str, reading_name: str,
    table: CalibrationTable, conn: sqlite3.Connection,
) -> int:
    """流式校验读数、换算校时并写入磁盘 SQLite，随后在磁盘上标记撞秒。"""
    fh = open(reading_path, "r", encoding="utf-8-sig", newline="")
    batch: list[tuple] = []
    seq = 0
    try:
        reader = csv.reader(fh)
        try:
            header = next(reader, None)
        except UnicodeDecodeError as exc:
            raise CorrectionError(
                f"文件 {reading_name} 编码不是 UTF-8：{exc}"
            ) from exc
        idx = _header_index(header, READING_HEADERS, reading_name)
        try:
            for raw in reader:
                line_no = reader.line_num
                if not raw or all(cell.strip() == "" for cell in raw):
                    raise _bad_row(reading_name, line_no, "存在空行")
                if len(raw) != len(idx):
                    raise _bad_row(
                        reading_name, line_no,
                        f"列数应为 {len(idx)}，实际 {len(raw)}",
                    )
                try:
                    meter_id = _parse_nonneg_int(raw[idx["meter_id"]])
                    boot_id = _parse_nonneg_int(raw[idx["boot_id"]])
                    uptime_ms = _parse_nonneg_int(raw[idx["uptime_ms"]])
                    word_high = _parse_word(raw[idx["word_high"]])
                    word_low = _parse_word(raw[idx["word_low"]])
                except ValueError as exc:
                    raise _bad_row(reading_name, line_no, str(exc)) from exc

                seq += 1
                if seq > MAX_READING_ROWS:
                    raise _bad_row(
                        reading_name, line_no,
                        f"读数超过 {MAX_READING_ROWS} 条上限",
                    )

                # 与 /read 相同的高字在前解码
                cumulative = (word_high << 16) | word_low
                second = table.correct_second(boot_id, uptime_ms)
                if second is None:
                    reason = (
                        REASON_NO_CALIBRATION
                        if not table.has_boot(boot_id)
                        else REASON_OUT_OF_RANGE
                    )
                    batch.append(
                        (seq, line_no, meter_id, cumulative, None,
                         STATUS_REVIEW, reason)
                    )
                else:
                    batch.append(
                        (seq, line_no, meter_id, cumulative, second,
                         STATUS_OK, None)
                    )
                if len(batch) >= INSERT_BATCH:
                    conn.executemany(
                        "INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?, ?)", batch
                    )
                    conn.commit()
                    batch.clear()
            if batch:
                conn.executemany(
                    "INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?, ?)", batch
                )
                conn.commit()
        except csv.Error as exc:
            raise _bad_row(reading_name, reader.line_num,
                           f"CSV 解析失败：{exc}") from exc
        except UnicodeDecodeError as exc:
            raise CorrectionError(
                f"文件 {reading_name} 编码不是 UTF-8：{exc}"
            ) from exc
    finally:
        fh.close()

    # 撞秒检测完全交给磁盘索引：同表同秒多于一笔则全部待核对
    # （不区分启动编号；无法换算的行 corrected_second 为 NULL，不参与）
    conn.execute("CREATE INDEX idx_meter_second ON readings(meter_id, corrected_second)")
    conn.execute(
        "UPDATE readings SET status = ?, reason = ? "
        "WHERE rowid IN ("
        "  SELECT r.rowid FROM readings r"
        "  JOIN ("
        "    SELECT meter_id, corrected_second FROM readings"
        "    WHERE corrected_second IS NOT NULL"
        "    GROUP BY meter_id, corrected_second HAVING COUNT(*) > 1"
        "  ) c ON c.meter_id = r.meter_id"
        "     AND c.corrected_second = r.corrected_second"
        ")",
        (STATUS_REVIEW, REASON_COLLISION),
    )
    conn.commit()
    return seq
