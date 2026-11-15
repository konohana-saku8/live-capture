# 开发指南

## 环境

- **host（Windows）**：`.venv-host/` 用于跑测试（flask + waitress + websockets）；Node 用于前端构建。
- **手机**：`.venv` 是唯一装了 Flask 依赖的环境，跑服务用它；系统 python3 只装了 pip 和 websockets。
- 仓库有 `.gitattributes` 强制 LF——Windows checkout 出 CRLF 会让 Termux 脚本挂掉。**用 Edit 工具改过 `web/index.html` / shell 脚本后提交前要检查并还原 LF**。

## 三套测试（改动后都要跑）

| 测试 | 命令 | 覆盖 |
|---|---|---|
| 全量自测 | `.venv-host/Scripts/python.exe selftest.py` | 229 项：协议解析、store、账号、房间、采集链路（mock WebSocket）、定时调度、导入、CPU 读数、HTML 断言 |
| Flask 契约 | `.venv-host/Scripts/python.exe tests/test_flask_api.py` | 18 项：路由行为、鉴权（header/query）、400 校验、SPA fallback、场次删除 |
| CDP 界面检查 | `node .state/ui-check-flask.js`（先起本地服务） | 12 项：门禁、四页渲染、无 JS 异常、窄屏无溢出 |

注意：自测里同一房间的「开播」事件是跨阶段累计的，新增断言要用**差值**而不是绝对值。

## 改后端

- 业务层（store/capture/rooms/accounts/dynamics）保持框架无关，不要引入 Flask 对象。
- HTTP 层：新路由放 `app/api/` 对应域的蓝图；鉴权/错误/请求体统一走 `app/http.py`（`@body`、`ApiError`、`int_arg` 等）。配置类校验失败一律 400，不要让它变成 500。
- 改 `store.py` 表结构 → `MIGRATIONS` 加 `ALTER TABLE`，禁止重建表。
- `capture.py` 的关键不变量：
  - `live_only` 的连接门禁：`live_status==1 且 live_time>0`，探状态失败（异常）也当没开播——见 pitfalls.md 的两次幻影开播事故；
  - LIVE 推送与补记 3 分钟去重；
  - `store.insert_event` / `insert_danmaku` 的字段结构变了要同步 selftest 的桩数据。

## 改前端（Vue 3 + TS）

```bash
cd frontend
node node_modules/vite/bin/vite.js build   # 产物输出 ../web/dist/（入库）
```

- `web/dist/` 是**入库**的（部署可复现），但 `node_modules/` 忽略。
- 旧视觉口径在 `frontend/src/styles/legacy.css`（原样抽取，别随手重构）；增量修补写 `app.css`。
- 新图片/外部资源一律走 `CdnImg`（`referrerpolicy="no-referrer"` + 失败切 `/api/emote` 代理）。
- 页面交互约定：采集器页局部标签不换页不改 hash；弹幕只按场次看；新增宽页面内容宽 ≤1000px、按钮靠左，避免右侧悬浮元素遮挡（如适用）。
- 界面验证用 CDP 脚本（参考 `.state/ui-check-flask.js`）：断言元素存在/文案/无 `document.documentElement.scrollWidth > innerWidth` 溢出、无 JS 异常。**headless Chrome 最小窗口宽约 500px，390px 截图右边被裁是工具假象**，量 scrollWidth 判断。

## 添加新的数据源 / 子标签（采集器页）

1. `RoomPanel.vue` 局部标签：`button[data-sub="x"]` + 对应面板；
2. 后端补 `/api/...` 数据与按 `room_id`/`mid` 过滤；
3. store 加载分支补一行；
4. selftest / CDP 各补断言。

## 提交纪律

- 提交前确认：LF 无 CRLF、`git status` 无 node_modules/构建临时物混入。
- 一次功能一个提交，消息写清「做了什么 + 为什么」。
- 本仓库无远端，发布 = scp 推文件（见 operations.md）。
