# 数据模型

SQLite 单文件：`data/capture.db`（另有 WAL）。表结构变更**必须**走 `store.py` 的 `MIGRATIONS` 做 `ALTER TABLE`——手机上的旧库不能重建。

## 五张表

| 表 | 内容 | 主键/去重 |
|---|---|---|
| `danmaku` | 弹幕（含 `emote_url/emote_px/emote_map` 表情字段） | 内容哈希去重；`seq` 自增 |
| `sc_message` | 醒目留言（`price`、`message`、`message_trans` 官方日译） | `sc_id`（B 站 ID）去重 |
| `room_event` | 事件：`live`（开播/下播）、`gift`、`guard`、`sc_delete`… | `raw_json` 保留原始报文 |
| `dynamics` | up 主动态（`mid` 标识采集器，`fetched_at` 记录抓取时间） | `dyn_id` 去重 |
| `runs` | 采集批次（服务启动一次采集 = 一个 run），仅作兜底统计用 | — |

7 个索引覆盖按 `room_id`、`received_at` 的常规查询路径。

## 场次切分口径（`store.live_segments`）

- **场（kind=live）**：开播事件 → 下播事件（缺下播则到下次开播或至今）。
- **场间（kind=gap）**：上次下播 → 这次开播。
- **未分段（single/lead）**：一次开播都没记录时，所有数据归成一段「未分段」，等下一次真开播才切开——**不要**拿 runs（采集批次）冒充场次。
- 空场间（无任何互动）不返回，避免噪声。
- 每段带 `danmaku/sc/amount/viewers` 统计，前端按 `live-N` / `gap-N` 作为选择键。

## 关键时间字段

- `received_at`：**毫秒**时间戳，服务端收到/入库的时刻。所有分页游标（`before` / `since`）、场次边界、删除接口都用它。
- `event_time`：事件原始的秒级时间。补记「开播」时两者一致；按真实开播时间补记时 `event_time` 是真实值。
- `dyn_last_ts` / `fetched_at`：动态定时抓取的记账。

## 数据删除

- 唯一的删除入口是 `POST /api/session/delete`（按 received_at 时间段删三张表）与 `/api/clear`（按房间清空）。
- 历史导入 `import_history.py` 按**内容哈希**去重，可重复执行不产生重复行；`--gap 30` 按空档推断场次并补写开播事件；`--min-seconds` 跳过过短段。

## rooms.json（采集器配置）

每条采集器（按 up 主 uid）：

- `collect_live` / `collect_dynamics` / `live_only`（`COLLECT_KEYS`，默认 True/True/False）
- `params{}`：采集参数按 up 主覆盖全局
- `dyn_auto` / `dyn_hour` / `dyn_minute` / `dyn_last_ts`（`SCHED_KEYS`，定时抓取**默认关闭**、08:00）
- 资料：`label/uname/face/room_id/live_time` 等（添加时自动拉取，refresh 更新）
