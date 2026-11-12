"""百万行校时压力脚本：磁盘流式处理 + 内存占用观测（不在 requirements 内）。

用法：python3 scripts/stress_correction.py [行数]
"""

from __future__ import annotations

import io
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.correction import prepare_correction  # noqa: E402


def _rss_mib() -> float:
    # ru_maxrss 在 Linux 上单位为 KiB
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class _Upload:
    def __init__(self, data: bytes, name: str) -> None:
        self.filename = name
        self.file = io.BytesIO(data)


class _ChunkFile:
    """模拟 UploadFile.file：按块产出，调用方无法一次性读到全部内容。"""

    def __init__(self, chunks) -> None:
        self._it = iter(chunks)
        self._buf = b""

    def read(self, size: int = -1) -> bytes:
        want = size if size and size > 0 else 1 << 20
        while len(self._buf) < want:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out, self._buf = self._buf[:want], self._buf[want:]
        return out


def _reading_chunks(n: int):
    yield b"meter_id,boot_id,uptime_ms,word_high,word_low\n"
    buf: list[bytes] = []
    for i in range(n):
        meter = i % 50               # 50 块表
        uptime = (n - i) * 7         # 故意乱序
        word = i % 65536
        buf.append(
            f"{meter},1,{uptime},{word >> 16},{word & 0xFFFF}\n".encode()
        )
        if len(buf) >= 10_000:
            yield b"".join(buf)
            buf.clear()
    if buf:
        yield b"".join(buf)


def main(n: int) -> None:
    # 校时区间覆盖全部 uptime；斜率 1:1，uptime//1000 制造大量同表同秒
    max_uptime = n * 7 + 1
    cal = (
        "boot_id,uptime_ms,utc_ms\n"
        f"1,0,1700000000000\n"
        f"1,{max_uptime},{1_700_000_000_000 + max_uptime}\n"
    )

    class _ChunkedUpload:
        filename = "readings.csv"

        def __init__(self) -> None:
            self.file = _ChunkFile(_reading_chunks(n))

    t0 = time.perf_counter()
    job = prepare_correction(_ChunkedUpload(), _Upload(cal.encode(), "calibration.csv"))
    t1 = time.perf_counter()

    out_rows = 0
    review_chunks = 0
    for chunk in job.stream():
        out_rows += chunk.count(b"\n")
        review_chunks += chunk.count("待核对".encode())
    job.close()
    t2 = time.perf_counter()

    print(f"读数行数: {n}，输出数据行: {out_rows - 1}")
    print(f"准备阶段(落盘+校验+撞秒索引): {t1 - t0:.1f}s")
    print(f"流式输出阶段: {t2 - t1:.1f}s，总耗时: {t2 - t0:.1f}s")
    print(f"进程峰值 RSS: {_rss_mib():.1f} MiB（容器硬上限 192 MiB）")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    main(n)
