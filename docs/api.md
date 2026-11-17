# API 一览

Base URL：手机局域网 `http://192.168.10.107:8765`。

## 鉴权

- 除「公开路由」外全部需要访问口令：请求头 `X-Access-Token: <token>` 或 `?token=<token>`。
- 公开路由：`GET /api/health`、`GET /api/gate`、`GET /api/emote`。
- 口令在配置页设置，存 `data/config.json`（API 返回中永不回显明文，只有 `token_set`）。

## GET 路由

| 路由 | 说明 |
|---|---|
| `/api/health` | 存活探针（免口令），返回 `{ok:true, ...}` |
| `/api/gate` | `{need: bool}` 是否已设口令 |
| `/api/status` | 采集快照：`capture{rooms[], active, counts, problems}` |
| `/api/dashboard` | 总数 / SC 金额 / 按房间统计（含 `last_live_at`）/ 高额 SC / 最近 / 12h 时间轴 |
| `/api/rooms` | 采集器清单，每项含开关、params、定时配置、主播资料、实时状态，以及「全局默认 + 本采集器覆盖」合并后的 `record_*` 内容开关 |
| `/api/feed` | 内容流。参数：`room_id`、`types`（逗号分隔）、`paged=1&limit=N`（数据页默认取最新一页）、`before=<received_at>&incl=1`（数据页往上滑追加更早页）、`after=<received_at>&order=asc&incl=1`（直播增量追加新消息；`incl=1` 含游标毫秒）、`all=1`（整段一次拉取，脚本 / 预览用；单场上限 10 万条）、`since=` / `until=`（左闭右开）、`q=`（搜索） |
| `/api/sessions` | 场次列表（`room_id`）；`with_recordings=1` 时给每个 `live` 场次挂 `recording`（录像状态 / 清晰度 / MP4 分片），见 data-model.md 的切分口径 |
| `/api/recordings` | 录像列表：`room_id`、`from`、`to`、`state`；同时返回 `summary`（总大小 / 磁盘剩余 / 状态计数） |
| `/api/recordings/summary` | 录像汇总：`count`、`total_bytes`、`free_bytes`、`warn`、`by_status` |
| `/api/recordings/<id>/media` | 录像文件，`part=0&kind=mp4|flv`；支持 Range（206），路径必须在 `data/recordings/<room_id>/` 下 |
| `/api/live/flv` | 实时 FLV 同源代理（`room_id`、`qn`），给 mpegts.js 播放；同一房间最多 2 个观众 |
| `/api/danmaku` `/api/superchats` `/api/events` | 单类数据查询（分页、按房间过滤） |
| `/api/dynamics?mid=` | 某采集器的动态 + 该采集器的定时配置回填 |
| `/api/accounts` | 账号池（SESSDATA 掩码 + uid + profile） |
| `/api/config` | 当前配置（token 永不回显） |
| `/api/system` | 设备读数，CPU 带 `scope: system|process` 口径 |
| `/api/logs` | 日志环形缓冲（含采集、waitress / HTTP、异常；最多 2000 条），`limit` 可调 |
| `/api/logs/file` | 下载完整应用日志 `data/logs/service.log`（按 5MB 轮转） |
| `/api/export` | 导出 JSON / CSV；支持 `type`、`format`、`room_id`、`q`、`since`、`until`（左闭右开，数据页按选中段导出） |
| `/api/emote?u=` | 图片同源代理（免口令；白名单 `*.hdslb.com / *.bilibili.com / *.biliimg.com`，禁内网/非标端口/SVG，LRU 缓存，失败回退文字） |

## POST 路由

| 路由 | 说明 |
|---|---|
| `/api/rooms` | 添加采集器（按 **up 主 uid**，自动解析 roomid 与资料） |
| `/api/rooms/remove` | 移除采集器（不删数据） |
| `/api/rooms/refresh` | 重新拉取资料（标题 / 头像 / 直播状态） |
| `/api/rooms/settings` | 改单个采集器的开关与参数。白名单键：`collect_live`、`collect_dynamics`、`live_only`、`params{}`（含 `record_danmaku`、`record_sc`、`record_events`、`record_guard`、`record_gift`、`record_red_pocket`、`record_enter`、`record_interact`、`auto_record`、`record_qn`）、`dyn_auto`、`dyn_hour`、`dyn_minute`；必须带 `room_id` |
| `/api/recordings/<id>/remux` | 手动重试录像转封装 |
| `/api/capture/start` | 启动某采集器（`{"room_id": N}`；`live_only` 且未开播会 409 拒绝） |
| `/api/capture/stop` | 停止某采集器 |
| `/api/capture/stop_all` | 全部停止 |
| `/api/session/delete` | **按时间段删数据**：`{room_id, start, end, kind}`。`kind=live` 连开播/下播标记一起删（之后归「未分段」）；`kind=gap` 只删内容、标记保留（场变空）。删除范围覆盖 danmaku / sc_message / room_event 三张表 |
| `/api/dynamics/fetch` | 立即抓一次动态（必须带 `room_id`） |
| `/api/dynamics/config` | 设定时抓取（必须带 `room_id`，否则 400） |
| `/api/dynamics/trans` | 补写动态译文 |
| `/api/dynamics/delete` | 删一条动态 |
| `/api/accounts` | 添加账号（粘贴 cookie CSV） |
| `/api/accounts/remove` `/api/accounts/activate` | 删除 / 启停用（空 id = 停用） |
| `/api/config` | 保存配置（`token` / `port` 等有范围校验，非法返回 400） |
| `/api/clear` | 清空数据（必须 `confirm` + 可按 `room_id`） |

## 兼容别名

`/api/start` = `/api/capture/start`，`/api/stop` = `/api/capture/stop`（`add_url_rule` 注册）。

## 语义要点

- **分页约定**：`/api/feed?paged=1&limit=400` 返回 `items/counts/more`；「载入更早」传 `before=<首条 received_at>&incl=1`（含同一毫秒，前端按主键去重）。
- **错误格式**：统一 `{"ok": false, "error": "..."}`，校验失败 400，无房间 404/409，口令错误 401，请求体过大 413。
- **每请求 512 KB 体积上限**（`app/http.py`）。
