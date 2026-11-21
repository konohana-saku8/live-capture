---
name: deploy-termux
description: 把 live-capture 代码改动发布到手机 Termux 并重启服务、验证健康。当用户说「部署」「更新到手机」「发布」或改动完成需要上线时使用。
---

# 部署 live-capture 到手机 Termux

目标环境：Android 10+ 手机上跑的 Termux，局域网 `http://<手机IP>:8765`。下文用 `phone` 代指 `~/.ssh/config` 里的手机别名（Termux sshd，默认 8022 端口）。部署目录 `~/live-capture`，运行解释器 `~/live-capture/.venv/bin/python`（**系统 python3 没装 Flask，禁止用它跑服务**）。

## 单文件后端改动

```bash
cd live-capture
scp -q <改动的 .py> phone:~/live-capture/<同名文件>
```

## 前端产物（frontend 构建后）

```bash
cd live-capture
ssh phone "rm -rf ~/live-capture/web/dist && mkdir -p ~/live-capture/web/dist"
scp -q -r web/dist/. phone:~/live-capture/web/dist/
```

坑：多源 `scp -r app run.py web/dist phone:~/live-capture/` 会把 `web/dist` 平铺成 `~/live-capture/dist`，要么单源推送、要么推完 `ssh phone "mv ~/live-capture/dist ~/live-capture/web/dist"`，并核对 md5。

## 重启服务（固定套路）

```bash
ssh phone "cd ~/live-capture && kill \$(cat data/service.pid) 2>/dev/null; sleep 2; \
  nohup .venv/bin/python run.py > data/service.log 2>&1 & echo \$! > data/service.pid; sleep 6; \
  curl -s -m 6 -o /dev/null -w 'health HTTP %{http_code}\n' http://127.0.0.1:8765/api/health; \
  tail -5 data/service.log"
```

## 验证清单

1. `health HTTP 200`（本机局域网 `curl http://<手机IP>:8765/api/health`，免口令）。
2. 日志出现「自动开始采集 <room_id>」（auto_start=true 会自动恢复采集）；若采集是手动停的状态则不算异常。
3. 新前端资源 hash 出现在 `curl http://<手机IP>:8765/ | grep -oE 'index-[A-Za-z0-9_-]+\.js'`。
4. 若推了前端：`ssh phone md5sum web/dist/assets/*` 与本地比对。

## 注意

- 重启会清 `data/service.log`，要留证据先 `scp` 回来。
- 每次发布后确认 `~/.bashrc` 自愈脚本仍指向 `.venv/bin/python run.py`（改启动方式时同步改它与仓库备份 `termux-boot/bashrc-snippet.sh`）。
- SSH 不可用时兜底：adb 触发 Termux 输入（`am start -n com.termux/.HomeActivity` → `input text` 空格用 `%s` → `keyevent 66`），执行结果看 `/sdcard/live-capture/DEPLOY_STATUS.txt`。
