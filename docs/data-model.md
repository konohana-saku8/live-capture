# 数据模型

SQLite 单文件：`data/capture.db`（另有 WAL）。表结构变更**必须**走 `store.py` 的 `MIGRATIONS` 做 `ALTER TABLE`——手机上的旧库不能重建。

## 六张表

| 表 | 内容 | 主键/去重 |
|---|---|---|
| `danmaku` | 弹幕（含 `emote_url/emote_px/emote_map` 表情字段） | 内容哈希去重；`seq` 自增 |
| `sc_message` | 醒目留言（`price`、`message`、`message_trans` 官方日译） | `sc_id`（B 站 ID）去重 |
| `room_event` | 事件：`live`（开播/下播）、`gift`（礼物 / 投喂）、`guard`（上舰）、`red_pocket`（红包）、`enter`（入场，默认关）、`follow/share` 等互动、`sc_delete` | `raw_json` 保留原始报文 |
| `dynamics` | up 主动态（`mid` 标识采集器，`fetched_at` 记录抓取时间） | `dyn_id` 去重 |
| `runs` | 采集批次（服务启动一次采集 = 一个 run），仅作兜底统计用 | — |
| `recordings` | 录像元数据：`started_at/stopped_at/status/actual_qn/flv_parts/mp4_parts/bytes` | `id` 自增；文件只存 `data/` 下的相对路径 |

8 个索引覆盖按 `room_id`、`received_at` 的常规查询路径。

## 录像（recordings）

- 录制文件在 `data/recordings/<room_id>/`，DB 只存相对路径，整个 `data/` 可以搬走。
- `status`：`recording`（录制中）→ `pending`（待 remux）→ `remuxing` → `ready`（MP4）；没有 ffmpeg 时停在 `flv`，失败为 `failed`。
- `flv_parts` / `mp4_parts` 是 JSON 数组，每项带 `path/started_at/bytes/duration_ms`；断流续录会产生多个分片。
- `/api/sessions?with_recordings=1` 按 `live_started_at`（其次 `started_at`）与场次起点最近匹配，容差 10 分钟。
- 服务启动时 `RecordingService.reconcile()` 扫目录补录 DB、把上次中断的 `recording` 重新排队 remux。


## 场次切分口径（`store.live_segments`）

- **场（kind=live）**：开播事件 → 下播事件（缺下播则到下次开播或至今）。区间统一左闭右开 `[start, end)`，因此下播事件用 `end = 下播时刻 + 1ms` 归在场内，不落到场间。
- **场间（kind=gap）**：上一次下播之后 → 这次开播。
- **未分段（single/lead）**：一次开播都没记录时，所有数据归成一段「未分段」，等下一次真开播才切开——**不要**拿 runs（采集批次）冒充场次。
- **历史导入的开播入口**：优先取数据簇内最早 SC / 上舰（事件时间与 received_at 取更早，保证付费记录本身落在场内），两者都没有才回退到首条弹幕前 1 秒；下播仍取簇尾。`fix_live_starts.py` 用于修正旧导入数据。
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
- `params{}`：采集参数按 up 主覆盖全局；`auto_record` 开启后开播自动录 FLV（默认 False），`record_qn` 选清晰度（80/150/250/400/10000，默认 250=720P；实际值看 `actual_qn`）。内容开关：`record_danmaku` / `record_sc` / `record_events`（总开关），事件细类 `record_guard`（上舰）、`record_gift`（礼物/投喂）、`record_red_pocket`（红包）、`record_enter`（入场，默认 False）、`record_interact`（关注/分享等）。`/api/rooms` 返回的是全局默认与该采集器覆盖合并后的有效值。
- `dyn_auto` / `dyn_hour` / `dyn_minute` / `dyn_last_ts`（`SCHED_KEYS`，定时抓取**默认关闭**、08:00）
- 资料：`label/uname/face/room_id/live_time` 等（添加时自动拉取，refresh 更新）
