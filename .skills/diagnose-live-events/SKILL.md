---
name: diagnose-live-events
description: 排查 live-capture 场次/开播事件异常——幻影「开播」、缺下播、场次被切开、分段错乱。当用户报告「明明没开播却记了开播」「场次不对」「多出一段」时使用。
---

# 排查场次 / 开播事件异常

背景：房间若开 `live_only`，worker 每 45 秒探一次状态；「开播」记录有三个来源——状态探测补记（`_note_live_start`）、真实 LIVE 推送、历史导入。出问题先定位来源。

## 第一步：还原时间线

```bash
ssh phone "grep -n -B3 -A3 '<出事时刻 HH:MM:SS>' ~/live-capture/data/service.log; \
  cd ~/live-capture && .venv/bin/python << 'PYEOF'
import sqlite3
c = sqlite3.connect('data/capture.db')
for r in c.execute(\"select seq, room_id, kind, event_time, received_at, detail, run_id, import_key from room_event where room_id=? and received_at between ? and ? order by received_at\", (ROOM_ID, START_MS, END_MS)):
    print(r)
PYEOF"
```

判读要点：
- `event_time == received_at`（毫秒/秒差一致）→ **当场补记**的，不是按真实开播时间回填的；
- 「已连接 → 补记开播 → 已下播断开」出现在同一秒、且库里无配对下播 → 幻影；
- `run_id` 为空 + `import_key` 非空 → 历史导入产生的，核对导入参数。

## 已知根因（都修过，复发说明有新路径）

| 形态 | 根因 | 修复位置 |
|---|---|---|
| 状态误报 | B 站返回 `live_status=1` 但 `live_time=0`（"0000-00-00"） | `capture.py::_main` 门禁：`live_status==1 且 live_time>0` 才连 |
| 探测失败 | `resolve_room` 抛异常，旧兜底「照连别饿死」连上即断 + 补记 | 同一门禁：探失败 = 没开播，等 45 秒重试 |
| 补记 + 推送重复 | 连上时补记了开播，B 站又推真实 LIVE | LIVE 分支 3 分钟去重（`last_live_mark`） |

**当前有效的不变量**：`live_only` 下连接门禁是 `live_on = live_status==1 and live_time>0`，且探状态异常也走等待分支。改这段代码前读 `docs/pitfalls.md` 第 1 节。

## 处理幻影记录

1. 网页：「数据」页的场次卡片右侧 ✕ → confirm（删「场」会连开播/下播标记一起删，之后归「未分段」）。
2. 接口：`POST /api/session/delete {"room_id": N, "start": <ms>, "end": <ms>, "kind": "live"}`（带口令 header）。先 `select count(*) from danmaku where received_at between ...` 确认没误删真数据。
3. 删完看 `/api/sessions` 确认分段恢复。

## 验证修复

- `selftest.py` 里 `RaisingApi` 用例覆盖「探状态失败不补记」；`FlippedApi` 用例覆盖「live_time=0 不连」。新增路径要各补一个桩用例（用**差值**断言事件数，同房间的事件跨阶段累计）。
- 修完部署重启后观察一个探测周期（≥45 秒），确认无幻影记录产生。
