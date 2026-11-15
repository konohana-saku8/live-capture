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
| `/api/rooms` | 采集器清单，每项含开关、params、定时配置、主播资料、实时状态 |
| `/api/feed` | 内容流。参数：`room_id`、`types`（逗号分隔）、`paged=1&limit=N`（服务端分页）、`before=<received_at>&incl=1`（向前游标翻页）、`since=`（增量刷新）、`q=`（搜索） |
| `/api/sessions` | 场次列表（`room_id`），见 data-model.md 的切分口径 |
| `/api/danmaku` `/api/superchats` `/api/events` | 单类数据查询（分页、按房间过滤） |
| `/api/dynamics?mid=` | 某采集器的动态 + 该采集器的定时配置回填 |
| `/api/accounts` | 账号池（SESSDATA 掩码 + uid + profile） |
| `/api/config` | 当前配置（token 永不回显） |
| `/api/system` | 设备读数，CPU 带 `scope: system|process` 口径 |
| `/api/logs` | 日志（服务端日志 + 网页操作日志） |
| `/api/export` | 导出 JSON / CSV |
| `/api/emote?u=` | 图片同源代理（免口令；白名单 `*.hdslb.com / *.bilibili.com / *.biliimg.com`，禁内网/非标端口/SVG，LRU 缓存，失败回退文字） |

## POST 路由

| 路由 | 说明 |
|---|---|
| `/api/rooms` | 添加采集器（按 **up 主 uid**，自动解析 roomid 与资料） |
| `/api/rooms/remove` | 移除采集器（不删数据） |
| `/api/rooms/refresh` | 重新拉取资料（标题 / 头像 / 直播状态） |
| `/api/rooms/settings` | 改单个采集器的开关与参数。白名单键：`collect_live`、`collect_dynamics`、`live_only`、`params{}`、`dyn_auto`、`dyn_hour`、`dyn_minute`；必须带 `room_id` |
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
