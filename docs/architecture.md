# 架构

## 进程与线程模型

整个服务是**一个 Python 进程**（`run.py` 启动），无外部数据库、无消息队列。

| 组成 | 形态 |
|---|---|
| HTTP 服务 | `waitress.serve`（未安装则退回 Werkzeug），监听 `0.0.0.0:8765`，16 线程 |
| `DynamicScheduler` | 常驻 daemon 线程，每 30 秒 `_tick()` 对表，只拉到点的采集器的动态 |
| `NotifierScheduler` | 常驻 daemon 线程，每 5 秒对表；开播按 `notify_live_seconds`（默认 60）探状态，动态按 `notify_dyn_seconds`（默认 300）轮询，有新变化推飞书 |
| `RoomWorker × N` | **每个直播间一条 daemon 线程**，线程内 `asyncio.run()` 跑 websockets 长连接 |
| 共享状态 | `CaptureManager` 持一把 `RLock` 保护 `workers` 与各自 `state` 字典；HTTP 侧只读快照 |

`run.py` 只做装配：Config / RoomStore / AccountStore / Store / CaptureManager 交给 `app.create_app()`。

## 模块分工

| 文件 | 职责 |
|---|---|
| `app/__init__.py` | Flask 应用工厂：装配蓝图、静态托管（`web/dist/`）+ SPA fallback、公开路由白名单 |
| `app/http.py` | 统一鉴权（`X-Access-Token` header / `?token=` query）、`ApiError` → JSON 错误、`@body` 请求体解析 |
| `app/api/*.py` | Blueprint 按域分组：public / overview / rooms / capture / feed / recordings / dynamics / accounts / admin |
| `app/scheduler.py` | `DynamicScheduler`：按 up 主的定时动态抓取 |
| `capture.py` | B 站协议：封包编解码、心跳、zlib 解压（protover=2）、`BiliApi`、`RoomWorker`、`CaptureManager` |
| `store.py` | SQLite 六张表 + `MIGRATIONS` 增量迁移 + 场次切分 + 游标分页查询 |
| `recordings.py` / `remux.py` | 录像元数据、场次匹配、启动对账；单线程 remux 队列（`-c copy` 转 MP4） |
| `rooms.py` | `data/rooms.json`：采集器（up 主）清单，开关、参数、定时配置 |
| `accounts.py` | B 站 cookie 账号池（CSV 解析、profile 补拉、启停用） |
| `dynamics.py` | up 主动态抓取 |
| `notify.py` | 飞书自定义机器人推送（卡片构造 + 签名校验），只用标准库 |
| `app/notifier.py` | `NotifierScheduler`：开播 / 新动态判重与推送，状态存 `data/notify-state.json` |
| `images.py` | 表情/头像同源代理（`/api/emote`），绕 B 站 CDN 防盗链 |
| `sysinfo.py` | 设备读数（Android 上 `/proc/stat` 不可读，退化为本进程单核占用） |
| `import_history.py` | 从 `translate_history.db` 导入历史弹幕（内容哈希去重、空档推断场次） |
| `frontend/` | Vue 3 + TS 源码（Vite 构建，产物输出 `web/dist/`） |

## 一条弹幕的路径

```
B站 WSS 推送
  → RoomWorker 收包（16 字节包头分帧，protover=2 zlib 解压出嵌套包）
  → _handle_frame → _handle_message（按 cmd 分发）
  → 内存缓冲 _dm_buf，满 50 条或心跳时 _flush_danmaku 批量落库
  → SQLite danmaku 表
  → 前端 /api/feed 游标分页（received_at 游标 + incl 去重）
```

开播检测：`live_only` 开启时每 45 秒（`WATCH_INTERVAL`）探一次状态 API，`live_status==1 且 live_time>0` 才连弹幕服务器并补记「开播」；下播由弹幕服务器 `PREPARING` 推送或 `_serve` 状态检查触发断开。LIVE/PREPARING 推送与补记之间有 3 分钟去重，防止同一场被切两半。

自动录制：采集器参数 `auto_record` 开启后，`LIVE`/补记开播/启动时已在播都会起一个独立录制线程，向 `playUrl` 取 HTTP-FLV 地址直接落盘到 `data/recordings/<room_id>/`，不需要 ffmpeg；`PREPARING`、手动停止、采集线程退出时关闭。断流会用新地址续录为 `_p2.flv` 等分片；剩余空间低于 1GB 时停止并报错，低于 2GB 页面预警。清晰度由 `record_qn` 指定（默认 250=720P），B 站未登录可能降级，`actual_qn` 记录真实值。

下播后处理：`RecordingService.finish()` 把录像标成 `pending` 并交给 `remux.py` 的单线程队列，用 `ffmpeg -c copy -movflags +faststart` 无损转成 MP4；成功后删除 FLV，失败保留原文件并标 `failed`；没有 ffmpeg 时停在 `flv`，前端用 mpegts.js 回退播放。服务启动时 `reconcile()` 会重新排队上次中断的录像。

数据页：`/live` 用 `/api/sessions?with_recordings=1` 的场 / 场间做日历索引，数据预览、场次删除和导出都在这里；录制中通过 `/api/live/flv` 同源代理 + mpegts.js 播实时画面，下播后播 `/api/recordings/<id>/media` 的 MP4；选中场次后弹幕 / SC / 事件默认取**最新一页**；滚到列顶时才向前**追加**更早的页，直播 / 尾部场间的新消息继续追加到列尾。所有滚动加载只往已有列表里加行，不整页替换。点击消息按 `received_at - recording.started_at` 跳转；回放当前播放位置对应的消息已加载时，跟随会把该消息固定在对应栏的第一行，手机窄高下能看到更多后续内容。页面顶部另有「up 主动态」标签，动态的预览、定时抓取和译文编辑也在这里；采集器页 `/rooms` 只保留抓取状态和开关。

## 鉴权与公开路由

- 除下列三个公开路由外，`/api/*` 一律要求访问口令（存 `data/config.json` 的 `token`）：
  - `GET /api/health` — 存活探针
  - `GET /api/gate` — 前端门禁探测 `{need: bool}`
  - `GET /api/emote?u=` — `<img>` 无法带 header，免口令（有域白名单）
- 口令校验失败返回 401，前端收到后自动退回门禁页。

## 前端结构（Vue 3 + TS）

- 五页路由：仪表盘 `/`、数据 `/live`、采集器 `/rooms`、配置 `/config`、日志 `/logs`
- 状态集中在 `frontend/src/stores/app.ts`（无 pinia，普通模块级响应式对象）
- 视觉沿用旧版深色暖橙口径（`legacy.css` 原样抽取，`app.css` 放增量修补）
- 组件：`LiveRoomView`（日历 + 画面 + 数据栏，状态在 `stores/liveRoom.ts`）、`LiveFeedPanel`（场次 + 三列流）、`RoomPanel`（采集器局部标签）、`DynamicsPanel`、`FeedColumn`、`CdnImg`（防盗链兜底）等
