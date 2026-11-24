# pctools — 在 PC 上重压录像

手机录下来的 MP4 码率普遍偏高（22 号那场 720p30、2.5 Mbps，48 分钟就是 1 GB）。
手机上解码都吃力，重编码更不现实。这个工具把片子拉到 PC、用桌面显卡压、逐项
校验后回传，手机端只做三件事：发文件、收文件、改一行元数据。

`pctools/core` 不依赖任何 GUI 库，图形界面和命令行共用同一套逻辑。

## 为什么走 HTTP 而不是 `adb pull`

试过了，走不通。`adb pull` 读不到 Termux 私有目录：

```
adb shell ls /data/data/com.termux/files/home/live-capture/data/recordings/
→ Permission denied            # adb shell 是 uid 2000，Termux 是 u0_a212
adb shell run-as com.termux …
→ run-as: package not debuggable
```

只能先复制到 `/sdcard` 再拉，多一次 1 GB 的内部读写、多占一份空间，反而更慢。

而且吞吐本来就不是瓶颈。同一根 USB、同样 80 MB：

| 链路 | 下载 | 上传 |
|---|---|---|
| `adb pull` / `push` | 36.4 MB/s | 27.5 MB/s |
| `adb forward` + HTTP | **37.1 MB/s** | — |
| `scp` over USB | 25.0 MB/s | — |

三条完全不同的协议速度几乎一致，说明上限在 USB 2.0 链路。`adb forward` 也不是
「多一层网络」：adbd 在 `127.0.0.1:8765` 上监听，数据走同一根 USB 的 adb 通道。

回传方向更是只能走 HTTP：`adb push` 只会裸写文件，没有原子替换、没有校验、
没有 `mp4_parts[].bytes` 回写，也没有前端缓存失效。

## 准备

**1. Python 环境**（单独一个，不碰项目里的 `.venv-host`）

```bash
python -m venv .venv-pc
.venv-pc/Scripts/python -m pip install -r pctools/requirements.txt
```

**2. ffmpeg**（必须同时有 `ffmpeg` 和 `ffprobe`）

放到 `pctools/vendor/ffmpeg/bin/`，或者：

- 在界面里点「选择…」指到 `ffmpeg.exe` 所在目录
- 设环境变量 `FFMPEG_PATH`
- 装到 PATH 里

探测顺序：配置 → `FFMPEG_PATH` → vendor → PATH。找不到就不放行任务——宁可
不给跑，也不要跑到一半发现编不了。

`pctools/vendor/` 不进 git，自己解一份静态包（gyan.dev 的 essentials build 就够）。

**3. 连上手机**

插 USB 即可。填 `127.0.0.1:8765` 时工具会自己执行 `adb forward tcp:8765`；
adb 不可用或没连 USB 时，回落到你填的备用地址（Wi-Fi）。

口令在手机的 `data/config.json` 里，填一次就记住了。

## 用法

界面：

```bash
.venv-pc/Scripts/python -m pctools
```

流程是 **扫描 → 看清单 → 试算（可选）→ 开始**。

- **扫描**：列出所有 `ready` 状态的分片，一片一行。
- **试算**：真的按当前参数压 30 秒，外推全片体积。省不到 15% 会自动标「跳过」，
  省得压完发现更大。
- **开始**：选中的行才处理；一行不选就处理全部。已完成和已跳过的不会再跑。
- **停止**：当前这一步做完就退，手机上不会留下半截文件。

命令行（没装 PySide6 或想挂计划任务时）：

```bash
.venv-host/Scripts/python -m pctools.cli --scan          # 只看清单
.venv-host/Scripts/python -m pctools.cli --dry --limit 1 # 试算一片
.venv-host/Scripts/python -m pctools.cli --run           # 全量处理
```

## 单片做了什么

```
下载（Range 续传） → ffprobe 源 → 试算 30 秒 → 编码 → 校验 → 分块上传 → commit
```

**校验三关**，任一不过就中止，手机上的原文件一个字节都不动：

- 视频帧数与源一致（`-fps_mode passthrough`，不插帧不丢帧）
- `duration` 与 `mp4_parts[idx].duration_ms` 差 ≤ 100 ms —— 弹幕轴按毫秒对齐，
  这条最关键
- 抽样 SSIM ≥ 0.97（可在界面关掉，快一点）

**回传是原子的**：先写到目标同目录的 `.incoming`，校验通过才 `os.replace`，
然后重算 `bytes`/`duration_ms` 并让前端缓存失效。

**原件留 7 天**：压好的原始下载文件放在 `~/.live-capture-pc/work/archive/<日期>/`，
误判了可以反向上传回去。改 `keep_days` 可调，0 表示不留。

## 开播时不会动

开始前查一次直播状态，在播就直接拒绝；每片开始前再查一次，发现开播就停在
片边界。采集和重压抢同一块闪存和同一根 USB，让采集优先。

## 配置

存在 `~/.live-capture-pc/config.json`，界面上改完会自动保存。

| 键 | 默认 | 说明 |
|---|---|---|
| `host` | `127.0.0.1:8765` | 走 adb 转发 |
| `fallback_host` | 空 | adb 不可用时的 Wi-Fi 地址 |
| `encoder` | `auto` | `nvenc` / `qsv` / `amf` / `x264` |
| `crf` | 23 | x264 用 CRF，硬件编码自动降 2 档等价 |
| `max_bitrate` | 1600 | kbps，0 表示不限峰值 |
| `min_bitrate` | 1200 | 低于此码率的分片直接标「跳过」 |
| `keep_days` | 7 | 本地原件保留天数 |
| `pause_when_live` | true | 开播时拒绝高 IO 任务 |
| `check_ssim` | true | 压完做整片 SSIM 比对 |

## 已知边界

- 只处理 `status=ready` 且不在 remux 中的分片，其余跳过。
- 一片一个状态机，串行跑。并发压多片会让显卡抢显存，反而慢。
- 压完时长必须和源一致，否则 commit 会拒收——这不是保守，是弹幕轴的需要。
