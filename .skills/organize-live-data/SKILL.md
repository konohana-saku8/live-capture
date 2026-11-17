---
name: organize-live-data
description: 整理 live-capture 采集到的直播数据——核对场次分段、查弹幕/SC/事件、删除脏数据段、导出 JSON/CSV、做场次统计。当用户说「整理数据」「导出」「统计场次」「把某天的数据拿出来」「删掉某段数据」时使用。幻影开播等场次异常的排查走 diagnose-live-events。
---

# 整理直播数据

数据在手机 `~/live-capture/data/capture.db`（SQLite，WAL）。服务一直在写，**直连只许 `mode=ro` 只读**；一切写操作（删除）走 HTTP API，不要在手机上直接 UPDATE/DELETE。

## 第一步：选数据通道

| 通道 | 适用 | 用法 |
|---|---|---|
| HTTP API（推荐） | 查询、导出、删段 | `curl -H "X-Access-Token: <口令>" http://192.168.10.107:8765/api/...`，口令 `1234` |
| ssh + 只读 sqlite | API 没覆盖的复杂查询 | `ssh v20 "cd ~/live-capture && .venv/bin/python -c \"...\""`，连接串 `sqlite3.connect('file:data/capture.db?mode=ro', uri=True)` |

路由明细见 `docs/api.md`。常用：

- `GET /api/sessions?room_id=N` —— 场次列表（场/场间/未分段 + 每段统计）
- `GET /api/feed?room_id=N&types=danmaku,sc,event&paged=1&limit=400` —— 内容流；向前翻页 `before=<最早 received_at>&incl=1`
- `GET /api/danmaku|superchats|events?room_id=N&q=&since=&until=` —— 单类查询
- `GET /api/export?kind=<danmaku|sc|event>&fmt=json|csv&room_id=N&since=` —— 导出
- `GET /api/dashboard` —— 总数 / SC 金额 / 12h 时间轴
- `POST /api/session/delete {"room_id":N,"start":<ms>,"end":<ms>,"kind":"live|gap"}` —— 删段

## 第二步：核对场次分段

口径（详见 `docs/data-model.md`）：

- **场（live）** = 开播事件 → 下播事件；**场间（gap）** = 下播 → 下次开播；空场间不返回。
- 一次开播都没记录 → 全部归「未分段」，等下次真开播才切开。**不要拿 runs（采集批次）当场次**。
- 历史导入的开播入口优先簇内最早 SC / 上舰（没有付费事件才回退首条弹幕）；旧数据可用 `fix_live_starts.py --data data --before YYYY-MM-DD` 修正。
- 所有边界都是 `received_at`（**毫秒**）。

核对动作：拉 `/api/sessions`，看每场的起止是否合理、统计是否与该段内容量级相符。发现多出来的一段先走 `diagnose-live-events` 定性（幻影 / 缺下播），再决定删不删。

## 第三步：删脏段（唯一合法入口）

```
POST /api/session/delete
{"room_id": 22748536, "start": 1731688000000, "end": 1731690000000, "kind": "live"}
```

- `kind=live`：内容 + 开播/下播标记一起删，删完该时段归「未分段」；
- `kind=gap`：只删内容，标记保留（场变空段）。
- 删之前先确认范围：`select count(*) from danmaku where received_at between start and end`，别误删真数据。删完拉 `/api/sessions` 复核。
- 整个房间清空另有 `POST /api/clear`（必须 `confirm`），慎用。

## 第四步：导出 / 统计

- 结构化导出用 `/api/export`（JSON/CSV，带 `q`/`room_id`/`since` 过滤）。
- 自定义统计用 ssh 只读连库自己聚合：按 `received_at` 毫秒过滤，`room_event.kind` 区分 live/gift/guard/red_pocket/enter/follow/share/sc_delete；SC 金额在 `sc_message.price`。事件细类按采集器配置：`record_guard` / `record_gift` / `record_red_pocket` / `record_enter` / `record_interact`，都在采集器页按 up 主勾选，`record_enter` 默认关。
- 网页的「数据」页支持按选中段导出（`/api/export` 的 `since`/`until`，左闭右开），并可直接 ✕ 删除整段；弹幕 / SC / 事件默认显示最新一页，往上滑追加更早页。
- 时间显示用本地时区；`event_time` 是秒、`received_at` 是毫秒，混用时先统一。

## 与 live-archive 的关系（别混库）

- **两套库并存**：手机的 `capture.db` 是 live-capture 自己的库；live-archive 的枢纽是 `D:/HibaneHinome/live-archive/.data/translation_history.db`（LiveTranslate 写逐字稿、danmaku-it 写弹幕/SC）。表结构同源（danmaku/sc_message/room_event），但**互不读写**，不要把一边的数据写进另一边当作「同步」。
- 给 live-archive 提供 SC 校对素材（摘录择段、机器译文校准）时：从本服务导出 SC 原文，由人工/会话在 live-archive 侧按其 AGENT.md 的摘录标准整理，不直接改 live-archive 的库。
- 历史库反向导入（旧 translate_history.db → 本服务）用 `import_history.py`，按内容哈希去重可重复执行，`--gap 30` 推断场次并补写开播事件。

## 红线

- 手机库直连只读（`mode=ro`），写操作一律走 API。
- 删除只走 `/api/session/delete`，先数条数再删，删完复核分段。
- `received_at` 毫秒、`event_time` 秒，所有时间段参数以 API 文档为准。
