# Modbus 电表试采与缓存读数校时接口

新电表接入能耗库前的配套服务，含两个接口：

1. **`POST /read`（试采）**：提交测点的保持寄存器起始地址（从 0 算）与字数
   （1 或 2），并给出设备的可读区间（含两端）与单次读取上限；接口自动规划
   读段、按方案读表，返回读段与无符号计数（双字按高字在前拼接）。
2. **`POST /correct`（校时）**：网关断网期间本地时钟走偏，缓存电表读数需要
   依据同网关的校时日志换算回 UTC 整秒，再送入每表每秒只收一笔的能耗库。
   上传同一网关的校时日志与缓存读数两份 CSV，返回按读数原序排列的结果 CSV。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- API：<http://localhost:8000>，Swagger UI：<http://localhost:8000/docs>
- 模拟电表：`localhost:1502`（容器内 `meter:502`），保持寄存器 0..99
- API 容器内存上限 **192 MiB**（`docker-compose.yml` 的 `mem_limit`），
  校时临时 SQLite / 结果 CSV / 上传假脱机文件落在独立卷 `correction-tmp`

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

## 接口说明

### `POST /read`

请求体：

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

### `POST /correct`

`multipart/form-data` 上传同一网关的两份 CSV（字段名分别为
`calibration` 与 `readings`）：

**校时日志 `calibration`**（最多 1000 行），表头固定：

| 列 | 含义 |
| --- | --- |
| `boot_id` | 启动编号 |
| `boot_ms` | 该启动下的开机毫秒（单调时钟，网关重启清零） |
| `utc_ms` | 校时点对应的 UTC 毫秒 |

同一次启动的校时点按开机毫秒排序后，`boot_ms` 与 `utc_ms` 都必须**严格
递增**，否则视为坏文件。

**缓存读数 `readings`**（最多 100 万行，允许乱序），表头固定：

| 列 | 含义 |
| --- | --- |
| `meter_id` | 表号 |
| `boot_id` | 启动编号 |
| `boot_ms` | 读数产生时的开机毫秒 |
| `word_high` / `word_low` | 两个 16 位无符号寄存器字，按 `/read` 同款**高字在前**拼成累计计数 |

换算与判定规则：

1. 只在**同一次启动的相邻校时点之间（含端点）**做线性换算，结果向下取整到
   UTC 整秒；用整数算术实现，无浮点误差：
   `utc_ms = u0 + (u1-u0)*(m-m0)//(m1-m0)`，校正秒 `= utc_ms//1000`；
2. 启动编号在校时日志中不存在、或开机毫秒落在该启动首个校时点之前 / 末个
   校时点之后的读数无法换算，状态置为**待核对**，校正秒留空；
3. 校正后**同表同秒出现多笔读数（含跨启动）**，相关各笔全部置为待核对；
4. 结果 CSV 按读数**原序**输出，列：
   `source_row,meter_id,total_count,corrected_second,status,review_reason`，
   其中 `source_row` 为读数文件物理行号（表头为第 1 行）。

错误：任一行出现坏数据（表头不符、列数不对、非整数、寄存器字超出
0..65535、校时列不严格递增、超行数上限等）一律返回 `422`，`detail` 含
**文件名与行号**，并丢弃所有临时数据，**不返回部分结果**。

#### 内存与流式设计（192 MiB 约束）

- 上传文件用 `SpooledTemporaryFile` 假脱机：小文件留内存，超过 1 MiB
  自动落到 `correction-tmp` 卷，不把整份上传驻留内存；
- 读数逐行流式解析，按批（5000 行）写入磁盘上的临时 SQLite，内存中不攒
  全量读数；
- 撞秒检测走 SQLite 的磁盘索引 `(meter_id, corrected_second)`，不维护内存
  撞秒表；
- 结果按 `source_row` 顺序从 SQLite 分批拉取、逐行流式写出，不缓存整份输出；
- 响应结束（或报错）后临时目录整体清理。实测 100 万行处理约 12 秒，
  服务进程峰值 RSS 约 76 MiB。

#### Swagger 试查（随附样例）

`samples/` 下提供两份样例，其中两个启动的 UTC 覆盖窗口故意重叠，制造了
**跨启动同表撞秒**（表 1001：boot 2 第 6 行与 boot 1 第 14 行都校正到
`1700000070` 秒，两笔均待核对），同时覆盖正常校正、区间外、未知启动等
情形。在 Swagger 的 `POST /correct` → Try it out 中分别选择
`samples/calibration_sample.csv` 与 `samples/readings_sample.csv` 后 Execute
即可；命令行：

```bash
curl -X POST http://localhost:8000/correct \
  -F "calibration=@samples/calibration_sample.csv;type=text/csv" \
  -F "readings=@samples/readings_sample.csv;type=text/csv"
```

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
.venv/bin/pytest            # 规划 + 校时单元/集成测试，无需 Docker
```

环境变量：`METER_HOST` / `METER_PORT` / `METER_SLAVE_ID` / `METER_TIMEOUT`
为 `/read` 请求体连接参数的默认值（Compose 中已注入）；校时相关：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CORRECTION_TMP_DIR` | `/tmp/correction` | 上传假脱机 / SQLite / 结果 CSV 落盘目录 |
| `CORRECTION_MAX_UPLOAD` | `268435456`（256 MiB） | 单个上传文件字节上限，超出返回 422 |

## 目录结构

```
app/main.py          FastAPI 接口：/read 读表执行、/correct 上传与流式响应
app/planner.py       读段规划（动态规划：次数最少 -> 寄存器总数最少）
app/correction.py    校时：日志解析、整数线性换算、SQLite 落盘、撞秒标记
simulator/           pymodbus 内存模拟电表
samples/             Swagger 试查样例（含跨启动同表撞秒）
tests/               规划、校时核心与 API 集成测试（内存模拟，无需 Docker）
docker-compose.yml   api + meter 两个服务，api 限内存 192 MiB 并挂临时卷
```
