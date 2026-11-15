---
name: bili-capture-protocol
description: live-capture 的 B 站弹幕采集内核知识——封包格式、protover、心跳、LIVE/PREPARING、SC 译文回填、账号 cookie、限流规避。修改 capture.py / accounts.py / images.py 前必读。
---

# B 站采集内核要点

## 协议

- WSS 地址经 `get_danmu_info` 获取，选 `wss://zj-cn-live-comet...:2245/sub` 一类 host。
- 封包：16 字节包头（pack 头 + body），OP 帧分类：心跳包 / 心跳回复（人气值）/ 消息帧（zlib 压缩的 JSON 数组）。
- **默认 protover=2（zlib）**——刻意选择，避免 Termux 编译 brotli；protover=3 且无 brotli 时自动退回 2 并置 warning。
- 心跳周期 30 秒；`stale_seconds`（默认 120）收不到任何数据判死重连；重连指数退避 1→60 秒。
- 依赖锁定 `websockets>=13,<18`。

## 开播判定（两次事故的教训，勿回退）

```
live_on = live_status == 1 and live_time > 0
```

- 真开播时 `live_start_time` 一定有值；误报时为 0/"0000-00-00"。
- `resolve_room` 抛异常 = 当没开播（旧兜底会制造幻影记录，已删）。
- `live_only` 模式：不满足就 `_wait_for_live()` + 睡 45 秒（`WATCH_INTERVAL`），完全不连弹幕服务器。
- 连上后 `_note_live_start`：上一条 live 标记不是「开播」时按 `live_time` 补记（拿不到用 now）；LIVE 推送分支有 3 分钟去重，防止同一场切两半。
- 下播：`PREPARING` 推送置 `live_status=0`，`_serve` 顶部检查断开回等待。

## SC 链路

- `SUPER_CHAT_MESSAGE` → `extract_sc` 直接入库（`sc_id` 去重）。
- `SUPER_CHAT_MESSAGE_JPN` 只带译文 → 按 `sc_id` 回填 `message_trans`；译文先到先进 `_pending_trans`（容量 200，满了清空）。
- `SUPER_CHAT_MESSAGE_DELETE` → `mark_sc_deleted` + 记 `sc_delete` 事件。

## 弹幕与表情

- `DANMU_MSG` → `build_danmaku`，`info` 数组解析昵称/文本/表情；表情字段 `emote_url/emote_px/emote_map`。
- 表情图直连会 403（CDN Referer 防盗链），前端一律 `referrerpolicy="no-referrer"`，失败切 `/api/emote` 代理（`images.py`：白名单 `*.hdslb.com/*.bilibili.com/*.biliimg.com`、带官方 Referer 重试、LRU 256 条/16 MB）。

## 资料与账号

- 房间资料：`room/v1/Room/get_info` + `getInfoByRoom`（`live_start_time`）；按 uid 添加走 `getRoomInfoOld?mid=` 解析 roomid。
- 主播/账号头像昵称：`x/web-interface/card?mid=` 公开接口优先（不依赖登录态），nav API 兜底。
- 账号 cookie 来自 CSV（Tab 分隔 Netscape/Chrome 布局或逗号两列，判据含 `SESSDATA`），启用账号的 cookies 全量注入 `BiliApi`，优先于手填三项。
- 动态定时抓取失败后 30 分钟内不重试（防风控）；每 30 秒对表逐位拉。

## 改动纪律

- 任何新探测/兜底逻辑，先问「失败路径会不会产生幻影事件或热循环」。
- selftest 的采集链路是 mock WebSocket（`frame_pool` + `FakeApi`），改协议/门禁必须同步改桩并补断言（事件数用差值断言）。
