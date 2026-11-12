"""FastAPI：Modbus 电表试采与缓存读数校时接口。"""

from __future__ import annotations

import os
import shutil
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .correction import CorrectionError, run_correction
from .planner import PlanError, Segment, plan_reads

DEFAULT_HOST = os.getenv("METER_HOST", "meter")
DEFAULT_PORT = int(os.getenv("METER_PORT", "502"))
DEFAULT_SLAVE = int(os.getenv("METER_SLAVE_ID", "1"))
DEFAULT_TIMEOUT = float(os.getenv("METER_TIMEOUT", "3"))
# 校时临时 SQLite / 结果 CSV 与上传假脱机文件的落盘目录（容器内挂载专用卷）
CORRECTION_TMP_DIR = os.getenv("CORRECTION_TMP_DIR", "/tmp/correction")
# 单个上传文件大小上限（默认 256 MiB），超出直接拒绝，避免无限落盘
MAX_UPLOAD_BYTES = int(os.getenv("CORRECTION_MAX_UPLOAD", str(256 * 1024 * 1024)))
# 上传文件在内存中保留的最大字节数，超出即假脱机到磁盘
SPOOL_THRESHOLD = 1 * 1024 * 1024


class PointIn(BaseModel):
    address: int = Field(..., ge=0, description="保持寄存器起始地址（从 0 算）")
    words: int = Field(..., ge=1, le=2, description="字数：1（单字）或 2（双字，高字在前）")


class ReadRequest(BaseModel):
    points: list[PointIn] = Field(..., min_length=1, description="待试采测点，至少 1 个")
    range_start: int = Field(..., ge=0, description="设备可读区间起始地址（含两端）")
    range_end: int = Field(..., ge=0, description="设备可读区间结束地址（含两端）")
    max_read: int = Field(..., ge=1, description="单次读取寄存器字数上限")
    host: str = Field(default=DEFAULT_HOST, description="Modbus TCP 设备地址")
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535, description="Modbus TCP 端口")
    slave_id: int = Field(default=DEFAULT_SLAVE, ge=1, le=247, description="从站 / 单元号")
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=30, description="连接与读取超时（秒）")

    @model_validator(mode="after")
    def _check_range(self) -> "ReadRequest":
        if self.range_end < self.range_start:
            raise ValueError("range_end 不能小于 range_start")
        return self


class PointOut(BaseModel):
    index: int = Field(..., description="测点在请求 points 中的下标（从 0 算）")
    address: int
    words: int
    value: int = Field(..., description="无符号计数；双字时按高字在前（big-endian）拼接")


class SegmentOut(BaseModel):
    start: int = Field(..., description="读段起始地址（含）")
    end: int = Field(..., description="读段结束地址（含）")
    count: int = Field(..., description="本次读取的寄存器字数")
    point_indexes: list[int] = Field(..., description="该读段覆盖的测点下标")
    registers: list[int] = Field(..., description="读回的原始 16 位寄存器值（按地址升序）")


class ReadResponse(BaseModel):
    request_count: int = Field(..., description="实际 Modbus 请求次数（已最少化）")
    total_registers: int = Field(..., description="读取寄存器总数（次数相同时已最少化）")
    segments: list[SegmentOut]
    points: list[PointOut] = Field(..., description="按请求下标返回的测点评值")


app = FastAPI(
    title="Modbus 电表试采与缓存读数校时接口",
    version="1.1.0",
    description=(
        "1. `POST /read`：提交测点保持寄存器起始地址（从 0 算）与字数"
        "（1 或 2），并给出设备可读区间（含两端）与单次读取上限，"
        "接口先规划读段（请求次数最少，次数相同则读取寄存器总数最少），"
        "再逐段读取，返回读段与无符号计数（双字高字在前）。\n\n"
        "2. `POST /correct`：上传同一网关的校时日志与缓存电表读数两份 CSV，"
        "按同次启动相邻校时点（含端点）线性换算到 UTC 整秒；无法换算或"
        "校正后同表同秒撞车（含跨启动）的读数标记为待核对，结果按读数"
        "原序以 CSV 流式返回。遇坏行直接报错，不返回部分结果。"
    ),
)


def decode_unsigned(words: list[int]) -> int:
    """无符号拼接；双字高字在前（big-endian / 高字在低地址）。"""
    value = 0
    for word in words:
        value = (value << 16) | (word & 0xFFFF)
    return value


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "modbus-meter-sampling", "docs": "/docs"}


@app.post(
    "/read",
    response_model=ReadResponse,
    summary="规划读段并对单台 Modbus TCP 电表执行试采",
    responses={
        422: {"description": "请求参数校验失败 / 无可行方案"},
        502: {"description": "连接或 Modbus 读取失败（不会返回部分读数）"},
    },
)
async def read_meter(req: ReadRequest) -> ReadResponse:
    try:
        segments: list[Segment] = plan_reads(
            points=[(p.address, p.words) for p in req.points],
            range_start=req.range_start,
            range_end=req.range_end,
            max_read=req.max_read,
        )
    except PlanError as exc:
        # 规划阶段失败：尚未与设备通信
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    client = AsyncModbusTcpClient(
        host=req.host,
        port=req.port,
        timeout=req.timeout,
    )
    try:
        await client.connect()
        if not client.connected:
            raise HTTPException(
                status_code=502,
                detail=f"连接 Modbus 设备失败：{req.host}:{req.port}",
            )

        seg_out: list[SegmentOut] = []
        # point_index -> 该测点读回的原始寄存器值
        point_words: dict[int, list[int]] = {}

        for seg in segments:
            try:
                rr = await client.read_holding_registers(
                    address=seg.start,
                    count=seg.count,
                    device_id=req.slave_id,
                )
            except (ModbusException, OSError, TimeoutError) as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"读取地址 {seg.start}..{seg.end} 失败：{exc}",
                ) from exc
            if rr.isError():
                raise HTTPException(
                    status_code=502,
                    detail=f"读取地址 {seg.start}..{seg.end} 失败：{rr}",
                )

            registers = list(rr.registers)
            seg_out.append(
                SegmentOut(
                    start=seg.start,
                    end=seg.end,
                    count=seg.count,
                    point_indexes=list(seg.point_indexes),
                    registers=registers,
                )
            )
            for idx in seg.point_indexes:
                p = req.points[idx]
                offset = p.address - seg.start
                point_words[idx] = registers[offset:offset + p.words]
    finally:
        client.close()

    points_out = [
        PointOut(
            index=i,
            address=p.address,
            words=p.words,
            value=decode_unsigned(point_words[i]),
        )
        for i, p in enumerate(req.points)
    ]
    return ReadResponse(
        request_count=len(segments),
        total_registers=sum(s.count for s in segments),
        segments=seg_out,
        points=points_out,
    )


def _spool_upload(file: UploadFile) -> tempfile.SpooledTemporaryFile:
    """流式读取上传文件：小文件留内存，超过阈值自动落盘到专用卷。

    返回的假脱机文件已 seek 到起始位置，可直接按行流式解析；
    调用方负责 close。超限抛出 422，不继续读取。
    """
    os.makedirs(CORRECTION_TMP_DIR, exist_ok=True)
    spool = tempfile.SpooledTemporaryFile(
        max_size=SPOOL_THRESHOLD,
        mode="w+b",
        dir=CORRECTION_TMP_DIR,
        prefix=f"upload-{file.filename or ''}-",
    )
    total = 0
    try:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=f"文件 {file.filename} 超过上传大小上限 "
                    f"{MAX_UPLOAD_BYTES} 字节",
                )
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool


@app.post(
    "/correct",
    summary="上传校时日志与缓存读数两份 CSV，输出校时后的结果 CSV",
    responses={
        200: {"content": {"text/csv": {}}, "description": "校时结果 CSV（按读数原序）"},
        422: {"description": "坏行 / 约束不满足（含文件名与行号），不返回部分结果"},
    },
)
def correct_meter_readings(
    calibration: UploadFile = File(
        ...,
        description=(
            "校时日志 CSV，表头 boot_id,boot_ms,utc_ms；"
            "最多 1000 行，同启动两列均严格递增"
        ),
    ),
    readings: UploadFile = File(
        ...,
        description=(
            "缓存读数 CSV，表头 meter_id,boot_id,boot_ms,word_high,word_low；"
            "最多 100 万行，可乱序；寄存器字为 16 位无符号值"
        ),
    ),
) -> StreamingResponse:
    cal_name = calibration.filename or "calibration.csv"
    rd_name = readings.filename or "readings.csv"
    cal_spool = _spool_upload(calibration)
    try:
        rd_spool = _spool_upload(readings)
    except BaseException:
        # 第二个文件假脱机失败（含超限）时，确保第一个也被清理
        cal_spool.close()
        raise

    os.makedirs(CORRECTION_TMP_DIR, exist_ok=True)
    tmp_dir: str | None = None
    try:
        try:
            output_path = run_correction(
                calibration=cal_spool,
                readings=rd_spool,
                calibration_filename=cal_name,
                readings_filename=rd_name,
                work_dir=CORRECTION_TMP_DIR,
            )
        except CorrectionError as exc:
            # 坏行：run_correction 已清理临时目录，不产出部分结果
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        tmp_dir = os.path.dirname(output_path)
    finally:
        cal_spool.close()
        rd_spool.close()

    def iter_result():
        try:
            with open(output_path, "rb") as result:
                while True:
                    chunk = result.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return StreamingResponse(
        iter_result(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="corrected_readings.csv"'
        },
    )
