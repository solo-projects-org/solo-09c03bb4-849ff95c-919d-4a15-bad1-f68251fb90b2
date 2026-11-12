# Modbus TCP 电表试采 / 缓存读数校时接口

两块能力：

1. **试采**：新电表接入能耗库前的单台 Modbus TCP 电表试采服务。厂务工程师提交
   测点的保持寄存器起始地址（从 0 算）与字数（1 或 2），并给出设备的可读区间
   （含两端）与单次读取上限；接口自动规划读段、按方案读表，返回读段与
   无符号计数（双字按高字在前拼接）。
2. **校时**：网关断网期间时钟走偏，对缓存的电表读数按校时日志线性换算校时，
   再送进每表每秒只收一笔的能耗库。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- API：<http://localhost:8000>，Swagger UI：<http://localhost:8000/docs>
- 模拟电表：`localhost:1502`（容器内 `meter:502`），保持寄存器 0..99
- API 容器内存硬上限 **192 MiB**（`docker-compose.yml` 的 `mem_limit`）

在 Swagger 中打开 `POST /read` → Try it out → 直接 Execute 即可对模拟表试采
（请求体默认指向容器内的模拟表）。也可在宿主机用 curl：

```bash
curl -X POST http://localhost:8000/read \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"address": 0, "words": 2},
      {"address": 2, "words": 2},
      {"address": 10, "words": 1},
      {"address": 12, "words": 1}
    ],
    "range_start": 0,
    "range_end": 99,
    "max_read": 8
  }'
```

## `POST /correct` 缓存读数校时

### 输入（同一网关的两份 CSV，`multipart/form-data` 上传）

| 表单字段 | 文件 | 表头 |
| --- | --- | --- |
| `readings` | 读数 CSV（最多 100 万条，可乱序） | `meter_id,boot_id,uptime_ms,word_high,word_low` |
| `calibration` | 校时日志 CSV（最多 1000 行） | `boot_id,uptime_ms,utc_ms` |

- 读数列为：表号、启动编号、开机毫秒、两个 16 位无符号寄存器字；
  累计计数沿用 `/read` 的**高字在前**解码：`word_high * 65536 + word_low`。
- 校时日志按开机毫秒排序；同一次启动内开机毫秒与 UTC 毫秒都必须**严格递增**。

### 校时规则

1. **只在同次启动的相邻校时点之间（含端点）线性换算**，区间外不外推；
   换算出的 UTC 毫秒**向下取整秒**（纯整数运算，无浮点漂移）。
2. 无法换算的读数（该启动无校时日志、或开机毫秒落在首末校时点之外）
   标为**待核对**，校正秒留空。
3. 校正后**同表同秒**的多笔读数全部标为**待核对**——即使启动编号不同
   （跨启动撞秒）也一样；待核对原因会注明撞秒。
4. 返回 CSV（带 UTF-8 BOM，Excel 可直接打开），按**读数原序**列出：
   `源行号, 表号, 累计计数, 校正秒, 状态, 待核对原因`。
5. 任何坏行（表头不符、缺列/多列、空行、非数字、寄存器字超 65535、
   校时序列未严格递增、超行数上限）直接返回 `422`，消息含**文件名与行号**，
   **不发部分结果**。

### Swagger 试查跨启动撞秒

仓库 `samples/` 内置两份样例，镜像内位于 `/code/samples/`：

```bash
curl -X POST http://localhost:8000/correct \
  -F "readings=@samples/readings.csv;type=text/csv" \
  -F "calibration=@samples/calibration.csv;type=text/csv"
```

样例中表 7 在启动 1、启动 2 的 `uptime=0` 都校正到同一 UTC 秒
`1700000000`，两行（源行 2、5）均判跨启动撞秒、全部待核对；
表 8 同理（源行 4、6）。另含正常插值、无校时日志、超出校时点覆盖范围、
32 位最大计数等场景。

### 内存与流式设计（百万行不攒内存）

- 上传先写入临时目录，逐行流式解析，**不在内存攒下全部读数**；
- 读数写入磁盘上的 SQLite，撞秒索引（`meter_id, corrected_second`）、
  排序与结果游标全部在磁盘侧；SQLite 页缓存限 8 MiB、临时对象落盘、
  关闭 mmap，**不建内存撞秒索引、不缓存输出**；
- 响应是分块 `StreamingResponse`，随查随发；请求结束（含异常）自动清理
  全部临时文件。实测 100 万行（含大规模撞秒）进程峰值 RSS 约 32 MiB，
  远低于 192 MiB 容器上限。复现：`python3 scripts/stress_correction.py 1000000`。

## `/read` 接口说明

### 请求体

| 字段 | 说明 |
| --- | --- |
| `points` | 测点列表，`address` 为保持寄存器起始地址（从 0 算），`words` 为字数（1 或 2） |
| `range_start` / `range_end` | 设备可读区间，含两端 |
| `max_read` | 单次读取寄存器字数上限 |
| `host` / `port` / `slave_id` / `timeout` | 设备连接参数，默认指向 Compose 中的模拟表 |

读段规划规则：

1. 每次读取一段连续寄存器，读段整体落在可读区间内，字数不超过 `max_read`；
2. 每个测点必须被某一次读取完整覆盖（不允许跨段拼接）；
3. 先使请求次数最少；次数相同时使读取寄存器总数最少
   （按地址排序后动态规划求解，读段紧贴测点，不多读无用寄存器）。

响应（200）：

- `request_count` / `total_registers`：实际请求次数与读取寄存器总数；
- `segments`：各读段的起止地址（含）、字数、覆盖的测点下标、原始寄存器值；
- `points`：按请求下标返回的测点值，`value` 为无符号计数，
  双字按高字在前（big-endian）拼接。

错误：

- `422`：参数校验失败或无可行方案（测点超出可读区间、字数超过单次上限等），
  `detail` 为中文原因；
- `502`：连接失败或 Modbus 读取失败（含设备异常响应），**不返回部分读数**。

## 模拟电表数据

`simulator/meter_sim.py`：保持寄存器 0..99，可读区间 `[0, 99]`。

| 地址 | 值 | 说明 |
| --- | --- | --- |
| 0..1 | 0x0001, 0x86A0 | 32 位无符号 100000（高字在前） |
| 2..3 | 0x0000, 0x0064 | 32 位无符号 100 |
| 10..13 | 1234, 5678, 65535, 42 | 单字演示值 |
| 其余 | `(addr * 37 + 11) & 0xFFFF` | 确定性填充，便于人工核对 |

## 本地开发（无 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
METER_PORT=1502 .venv/bin/python -m simulator.meter_sim &
METER_HOST=127.0.0.1 METER_PORT=1502 .venv/bin/uvicorn app.main:app --port 8000
.venv/bin/pytest            # 26 个用例：规划算法 + /read 集成 + 校时
```

环境变量：`METER_HOST` / `METER_PORT` / `METER_SLAVE_ID` / `METER_TIMEOUT`
为 API 请求体连接参数的默认值（Compose 中已注入）。

## 目录结构

```
app/main.py        FastAPI 接口：/read 读表执行 + /correct 校时上传
app/planner.py     读段规划（动态规划：次数最少 -> 寄存器总数最少）
app/correction.py  校时：流式 CSV 校验、线性换算、磁盘 SQLite 撞秒检测
simulator/         pymodbus 内存模拟电表
samples/           /correct 的 Swagger 试用样例（含跨启动撞秒）
scripts/           百万行内存/耗时压力脚本
tests/             规划单元测试 + 两个 API 的集成测试（无需 Docker）
docker-compose.yml api + meter 两个服务（api 限内存 192 MiB）
```
