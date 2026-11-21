# live-capture 文档

B 站直播弹幕 / SC / 礼物 / 动态采集服务，运行在手机 Termux 上，Flask 后端 + Vue 3 前端，数据落 SQLite，网页端管理与查阅。

## 文档索引

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 进程 / 线程模型、模块分工、一条弹幕的完整路径 |
| [api.md](api.md) | 全部 HTTP 路由、鉴权方式、分页与参数语义 |
| [data-model.md](data-model.md) | SQLite 五张表、场次（场 / 场间 / 未分段）切分口径、迁移规则 |
| [operations.md](operations.md) | 部署到手机、更新发布、SSH 接入、灭屏保活、故障恢复 |
| [development.md](development.md) | 本地开发、三套测试、前端构建、改代码的约定 |
| [pitfalls.md](pitfalls.md) | 踩坑积累：B 站接口怪癖、Android 限制、工具链坑、历史 bug 复盘 |

## 快速上手（AI 助手视角）

1. **改后端**：业务逻辑在 `store.py / capture.py / rooms.py / accounts.py / dynamics.py`，HTTP 层在 `app/`（Flask 蓝图）。改完必跑 `python selftest.py` + `python tests/test_flask_api.py`。
2. **改前端**：源码在 `frontend/`（Vite + Vue 3 + TS），构建产物输出 `web/dist/` 并入库。改完必跑 `node --check` 相关脚本 + CDP 界面检查。
3. **发布到手机**：scp 推文件 → 重启服务 → health 验证。详见 operations.md 与 `.skills/deploy-termux/`。
4. **排障**：先看 `data/service.log`（手机端）与 pitfalls.md 的历史案例。

## 一句话口径

- 数据只增不删（除非用场次删除接口）；服务重启自动恢复采集（`auto_start=true`）；
- 弹幕只按场次看，没有整库视图；采集器（up 主）级配置优先于全局；
- 线上以手机 Termux 里的部署目录为准，adb 只在 ssh 不可用时兜底。
