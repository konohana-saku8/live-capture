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

## 开播 / 动态推送

推送是**服务内的一个常驻线程**（`app/notifier.py`），和采集、定时抓动态互不影响：

1. 飞书群 → 设置 → 群机器人 → 添加「自定义机器人」，复制 webhook；
   开了「签名校验」的，把 secret 一起记下来。
2. 网页「配置」页 → 开播 / 动态推送 → 填 webhook（和 secret）→ 保存 → 「发一条测试消息」。
3. 网页「采集器」页 → 选中 up 主 → 直播区勾「开播推送」、动态区勾「新动态推送」。
   webhook 为空时全站不推送；单条采集器也可以单独关。

判重口径：开播按 B 站这一场的 `live_time`，动态按 `pub_ts`，第一次见到的
只记基线不推，所以服务重启不会把正在播的场次和历史动态重推一遍。
状态存 `data/notify-state.json`（删掉它就等于重新建立基线）。

开播探测走 `getInfoByRoom`（`live_start_time` 是 epoch，能和采集线程的口径对上），
匿名即可；**动态接口对匿名请求常返回 code=0 但空列表**，所以动态推送需要在
配置页放一个 B 站账号（采集器本来也要用它防 412）。拿不到数据时日志会写
「拉取动态返回 0 条」，不会静默失败。

## 快速上手（AI 助手视角）

1. **改后端**：业务逻辑在 `store.py / capture.py / rooms.py / accounts.py / dynamics.py`，HTTP 层在 `app/`（Flask 蓝图）。改完必跑 `python selftest.py` + `python tests/test_flask_api.py`。
2. **改前端**：源码在 `frontend/`（Vite + Vue 3 + TS），构建产物输出 `web/dist/` 并入库。改完必跑 `node --check` 相关脚本 + CDP 界面检查。
3. **发布到手机**：scp 推文件 → 重启服务 → health 验证。详见 operations.md 与 `.skills/deploy-termux/`。
4. **排障**：先看 `data/service.log`（手机端）与 pitfalls.md 的历史案例。

## 一句话口径

- 数据只增不删（除非用场次删除接口）；服务重启自动恢复采集（`auto_start=true`）；
- 弹幕只按场次看，没有整库视图；采集器（up 主）级配置优先于全局；
- 线上以手机 Termux 里的部署目录为准，adb 只在 ssh 不可用时兜底。
