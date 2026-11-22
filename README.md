# 预览片转码批次（FastAPI + FFmpeg）

剪辑组批量出预览片时，把三段无声短片转成统一规格，再按原顺序合成一个可下载 MP4。
某一段的“首跑”可以被人为放慢，用来演示补派与取消策略。

## 运行

```bash
docker compose up --build
```

打开 <http://localhost:8000>。首次启动会在 `/data/inputs` 生成三段内置无声片源（A/B/C，各 4 秒，640x360），
无需准备素材。数据卷 `preview-data` 会保留片源和产物。

### 演示“大量日志后失败”（维护员验收用）

```bash
docker compose --profile noisy-failure up --build
```

该模式把 `FFMPEG_BINARY` 指向 `app/demo_noisy_ffmpeg.py`：片段 B 的每次转码都会先向 stderr 写入
约 2 MiB 诊断（远超 Linux 管道约 64 KiB 的容量），再以退出码 1 失败；片源生成、探测、合成以及
A/C 两段仍透传给真实 ffmpeg。在网页上启动任意批次后可以观察到：

- 页面轮询全程不卡，其他批次照常派发、转码槽照常流转；
- 片段 B 的尝试标记为失败，失败详情包含进程退出前写入的最后一行日志；
- 详情带“日志已截断，仅保留末尾 4096 字节”注明，且只展示末尾 4 KiB 内容。

### 本机运行

本机不自带 Docker 时，需要 Python 3.11 + ffmpeg：

```bash
python3 -m pip install -r requirements-dev.txt   # 仅测试/本机演示需要 imageio-ffmpeg
python3 -m uvicorn app.main:app --port 8000
```

若 `PATH` 中没有 ffmpeg，应用会回退到 `imageio-ffmpeg` 自带的二进制；也可以用
`FFMPEG_BINARY=/path/to/ffmpeg` 显式指定。`FFMPEG_BINARY` 也支持带参数的命令前缀
（如 `python3 /app/app/demo_noisy_ffmpeg.py`），内部按 shell 词法拆分后以 argv 数组执行，不经过 shell。

## 页面怎么用

1. 选择“片段 A/B/C”中要模拟首跑变慢的那一段。
2. 点“启动转码批次”。可以连续起多个批次，它们共用全局两个转码槽。
3. 批次卡片实时显示每段的每次尝试：首跑 / 补派、状态、用时、是否仍占槽、失败原因。
4. 三段都产出选中结果后，卡片出现“下载成片 MP4”。

## 调度规则（对应需求）

| 需求 | 实现 |
| --- | --- |
| 各批次共用两个转码槽 | `BatchManager` 全局 `_slots_used`，槽数以活进程为准 |
| 优先派还没开跑的片段 | `pick_next_candidate`：所有未启动的首跑优先于任何补派 |
| 首跑超过 2 秒仍在运行，有空槽补派一次 | 每段最多一个 `backup`，阈值 `SLOW_AFTER_SECONDS` |
| 一段取第一份成功产物，取消余下尝试 | `_finalize_attempt` 选中后向同段其他尝试发取消 |
| 取消中的进程直到退出仍占槽 | `status=cancelling` 且 `slot_held=true`，退出时才减槽 |
| 迟到的产物不得覆盖已选结果 | 已选段的后续成功记为 `superseded`，删除其文件 |
| 三段齐全即按原顺序合成，可先于进程退出 | 合成在独立线程，仅读已选定产物 |
| 单次失败后继续等同段仍在运行的尝试 | 失败不会立刻判该段失败，只有无存活尝试才判失败 |
| 某段尝试全部失败则整批不提供成片 | 批次 `failed`，下载返回 409；同批其他段不再白跑 |
| stderr 只留末尾 4 KiB 且内存恒定 | `BoundedStderrCollector` 后台线程持续排空管道，环形缓冲只留尾部，截断时注明 |
| 日志量再大也不卡死 ffmpeg/调度 | stderr 走真实管道并被并发读空，输出超过管道容量也不会阻塞写入 |
| 失败详情等 stderr 收完再展示 | 失败路径 `finish()` 等待排空后再生成详情，含退出前最后一行；成功路径不等日志 |
| 进程退出即归还转码槽 | 还槽发生在等待日志排空之前，槽计数与活进程数保持一致 |

“模拟首跑变慢”不是 `sleep` 假动作：首跑会带 ffmpeg 的 `-readrate 0.4` 真实读入，
因此确实要跑约 10 秒（4 秒素材），产物仍是同一规格、可作为候选。补派走正常速度。

## 输出规格

所有尝试（含慢速首跑）都使用同一套参数：640x360、30fps、yuv420p、H.264 High@4.0、
固定 GOP 60、无音轨、faststart。合成使用 concat demuxer 直接流拷贝，因此在进程仍在退出时
也能立刻出片，且不会出现中途重编码。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `TRANSCODE_SLOTS` | `2` | 全局转码槽数 |
| `SLOW_AFTER_SECONDS` | `2.0` | 首跑超过该时长才可补派 |
| `SLOW_READRATE` | `0.4` | 慢速首跑的 ffmpeg `-readrate` |
| `MONITOR_INTERVAL` | `0.1` | 调度循环间隔（秒） |
| `CANCEL_GRACE_SECONDS` | `5` | 取消后多久升级为 SIGKILL |
| `CLIP_SECONDS` | `4` | 生成片源时长 |
| `STDERR_TAIL_BYTES` | `4096` | 每次尝试留存的 ffmpeg stderr 末尾字节数（环形缓冲，内存有界） |
| `STDERR_DRAIN_TIMEOUT_SECONDS` | `5.0` | 失败后等待 stderr 排空的上限（仅兜底，正常 EOF 近乎即时） |
| `DATA_DIR` | `data` | 片源/工作区/成片根目录 |
| `FFMPEG_BINARY` | 自动探测 | ffmpeg 可执行文件路径，或带参数的命令前缀（演示用） |

## 测试

```bash
python3 -m pytest tests -q
```

- `tests/test_scheduler.py` 用确定性的假 ffmpeg（`tests/fake_ffmpeg.py`）覆盖：正常出片、
  慢速补派、取消占槽、迟到产物、失败后继续等待、整批失败、全局两槽、合成顺序与未启动优先。
- `tests/test_stderr_capture.py` 直接对采集器做进程级验证：固定 4 KiB 上限、截断标记、
  超大输出下并发排空不阻塞，以及未超时不标记截断。
- `tests/test_scheduler_stderr.py` 覆盖调度器与采集器的协作：MiB 级日志不卡死且内存有界、
  失败详情等待排空并含退出前日志尾部、截断注明、成功不被日志拖慢、进程退出即还槽、
  其他批次与网页查询照常、首份有效产物即使 stderr 晚收完仍然获胜。
- `tests/test_api.py` 覆盖页面、创建/校验、轮询、下载与失败批次无成片，以及大日志期间
  HTTP 接口持续响应、截断后的失败详情经 API 可见。
- `tests/test_e2e_ffmpeg.py` 用真实 ffmpeg 跑通一次慢速 A 的完整批次，并校验成片时长/规格
  （本机需有 ffmpeg 或 `imageio-ffmpeg`）。

## 已知限制

- 调度状态在进程内存中，重启后历史批次与下载链接失效（磁盘产物保留）；因此只支持单 worker。
- 没有鉴权、持久化数据库和产物清理策略，定位是内部演示。
- 迟到的成功产物会记录为 `superseded` 并删除文件，界面上仍保留该次尝试明细。
