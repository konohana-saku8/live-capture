---
name: verify-changes
description: live-capture 改动后的测试与验证流程——selftest、Flask 契约测试、CDP 界面检查、本地实跑。任何代码改动完成、提交前、部署前都使用。
---

# 验证一套改动

## 顺序

1. **全量自测**（改了任何 .py）：
   ```bash
   cd /d/HibaneHinome/live-capture && .venv-host/Scripts/python.exe selftest.py
   ```
   当前基线 229 项全过。失败先看是不是桩数据要同步（字段变化、房间事件累计），再怀疑实现。

2. **Flask 契约测试**（改了 `app/`、`run.py`、路由行为）：
   ```bash
   .venv-host/Scripts/python.exe tests/test_flask_api.py
   ```
   当前基线 18 项。新路由必补：正常路径 + 400 校验路径 + 鉴权行为。

3. **前端语法/构建**（改了 `frontend/`）：
   ```bash
   cd frontend && node node_modules/vite/bin/vite.js build
   ```

4. **CDP 界面检查**（改了页面结构/交互）：
   - 本地起服务（后台方式）：`.venv-host/Scripts/python.exe run.py --data .state/testdata --host 127.0.0.1 --port 8766`，用 Bash 工具 `run_in_background`。
   - `node .state/ui-check-flask.js`（必要时按改动扩展断言）。
   - 断言必须包含：无 `window` JS 异常、关键元素/文案存在、`document.documentElement.scrollWidth <= innerWidth`（窄屏 390 与桌面 1280）。
   - **headless Chrome 最小窗口宽约 500px，390px 截图右边被裁是工具假象**；外网图片验证用 `--host-resolver-rules=MAP *.hdslb.com 127.0.0.1` 本地 mock 403（Chrome 不读 env 代理，要显式 `--proxy-server=http://127.0.0.1:9094`）。

5. **收尾**：测试完停掉本地服务（按端口找 PID taskkill）；检查 `git status` 无意外文件、shell 脚本/HTML 无 CRLF 混入，然后提交。

## 常见假失败

| 现象 | 真因 |
|---|---|
| selftest 某计数比预期多 1 | 同房间事件跨测试阶段累计，改用差值断言 |
| POST 400 但代码没问题 | 测试脚本自身笔误（内联 heredoc 打错字）；写进 tests/ 文件里跑 |
| Chrome 截图右边缺失 | 窗口最小 500px 的工具假象，量 scrollWidth |
| 界面检查里弹窗挡操作 | CDP `Page.javascriptDialogOpening` + `handleJavaScriptDialog` 处理 confirm |
| 端口被占 | 换端口（9399 等调试口同理） |
