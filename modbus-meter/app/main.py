"""FastAPI：单台 Modbus TCP 电表试采接口。"""

from __future__ import annotations

import os
from urllib.parse import quote

from anyio import to_thread
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .correction import (
    UTF8_BOM,
    CorrectionError,
    prepare_correction,
)
from .planner import PlanError, Segment, plan_reads

DEFAULT_HOST = os.getenv("METER_HOST", "meter")
DEFAULT_PORT = int(os.getenv("METER_PORT", "502"))
DEFAULT_SLAVE = int(os.getenv("METER_SLAVE_ID", "1"))
DEFAULT_TIMEOUT = float(os.getenv("METER_TIMEOUT", "3"))


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
    title="Modbus TCP 电表试采 / 缓存读数校时接口",
    version="1.1.0",
    description=(
        "两块能力：\n\n"
        "1. `POST /read`：提交测点保持寄存器起始地址（从 0 算）与字数（1 或 2），"
        "并给出设备可读区间（含两端）与单次读取上限。接口先做读段规划"
        "（请求次数最少，次数相同则读取寄存器总数最少），再逐段读取，"
        "返回读段与无符号计数（双字高字在前）。无可行方案或读取失败时直接报错，"
        "不返回部分读数。\n\n"
        "2. `POST /correct`：上传同一网关的读数 CSV 与校时日志 CSV，"
        "按同次启动相邻校时点线性换算校时（向下取 UTC 整秒），"
        "无法换算或校正后同表同秒撞秒（跨启动也算）的读数标为待核对，"
        "按读数原序返回结果 CSV。坏行报文件名与行号，不返回部分结果；"
        "最多 100 万条读数，全程磁盘流式处理，不把读数、撞秒索引或输出攒进内存。"
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


@app.post(
    "/correct",
    summary="上传读数 CSV 与校时日志 CSV，对网关缓存电表读数校时",
    responses={
        200: {"content": {"text/csv; charset=utf-8": {}}},
        422: {"description": "CSV 坏行 / 超行数上限（detail 含文件名与行号，不返回部分结果）"},
    },
)
async def correct_readings(
    readings: UploadFile = File(
        ...,
        description=(
            "读数 CSV，表头：meter_id,boot_id,uptime_ms,word_high,word_low"
            "（表号、启动编号、开机毫秒、两个 16 位无符号寄存器字）"
        ),
    ),
    calibration: UploadFile = File(
        ...,
        description=(
            "校时日志 CSV（最多 1000 行），表头：boot_id,uptime_ms,utc_ms；"
            "同次启动内开机毫秒与 UTC 毫秒均严格递增"
        ),
    ),
) -> StreamingResponse:
    # 落盘、全量校验与撞秒标记都是同步磁盘/CPU 操作，放线程避免阻塞事件循环；
    # 全部成功后才返回 StreamingResponse，任何坏行在这里就以 422 失败。
    try:
        job = await to_thread.run_sync(prepare_correction, readings, calibration)
    except CorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def payload():
        try:
            yield UTF8_BOM
            yield from job.stream()
        finally:
            job.close()

    filename = "corrected_readings.csv"
    return StreamingResponse(
        payload(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{filename}\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )
