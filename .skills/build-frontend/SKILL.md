---
name: build-frontend
description: live-capture 的 Vue 3 前端开发与构建发布流程。改 frontend/ 下任何文件、需要出新构建产物或推前端到手机时使用。
---

# 前端开发与构建（Vite + Vue 3 + TS）

## 结构

- 源码 `frontend/`：`src/views/`（四页）、`src/components/`、`src/stores/app.ts`（集中状态）、`src/api/client.ts`（口令存储 + fetch 封装 + 401 退门禁）。
- 样式：`src/styles/legacy.css` 是旧版视觉的**原样抽取**（深色暖橙口径），不要随手重构；增量修补写 `src/styles/app.css`。
- 构建产物输出 `../web/dist/`，**该目录入库**（部署可复现）；`node_modules/` 已忽略。

## 构建

```bash
cd /d/HibaneHinome/live-capture/frontend
node node_modules/vite/bin/vite.js build
```

（npm 直接调 `vite` 也行；依赖装好后产物在 `web/dist/assets/index-*.js|css`。）

## 发布到手机

```bash
ssh v20 "rm -rf ~/live-capture/web/dist && mkdir -p ~/live-capture/web/dist"
scp -q -r web/dist/. v20:~/live-capture/web/dist/
curl -s http://192.168.10.107:8765/ | grep -oE 'index-[A-Za-z0-9_-]+\.js'   # 确认新 hash 在线
```

## 开发约定（用户口径）

- 弹幕**只按场次查看**，没有整库视图；换采集器回到最近一段。
- 数据页 `/live` 是唯一的数据预览 / 管理入口：顶部标签分「直播场次」「up 主动态」；场次页有日历、删除整段、按段导出；消息默认显示最新一页，往上滑追加更早页（只加行，不替换）。
- 采集器页 `/rooms` 只显示抓取状态和开关，不再铺弹幕 / 场次 / 动态。
- 配置 / 开关 / 定时 / 启停**全部按 up 主**区分；定时抓取默认关闭（08:00）。
- 表情渲染成 `<img>`（CdnImg 兜底）；账号 chip 显示头像。
- 访问码不在 header 常驻输入框，只有「清除访问码」按钮；门禁流程：先隐藏界面 → 本地 token 验证 → 通过直进。
- 内容宽 ≤1000px、交互控件靠左，防右侧悬浮元素遮挡。

## 已知坑

- `Edit` 工具会把整档换成 CRLF：构建无关紧要，但提交前检查 `web/dist` 与源文件的换行。
- 依赖安装中断会造成 esbuild 平台二进制截断（构建报错时 `npm install @esbuild/win32-x64 --no-save` 重装）。
- 组件模板里 `label=""` 这类空属性传给需要非空 prop 的组件会构建失败，属性能从子组件内部推导就不要从父层传。
