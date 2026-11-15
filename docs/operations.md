# 运维手册（手机 Termux）

## 目标设备

| 项 | 值 |
|---|---|
| 手机 | 华为 TEL-AN00a / EMUI 10 / Android 10 / arm64 / 无 root |
| 局域网 | `192.168.10.107`，服务端口 `8765` |
| SSH | `ssh v20`（8022 端口，用户 `u0_a212`，公钥 `~/.ssh/id_rsa` 免密）；USB 兜底 `adb forward tcp:8022 tcp:8022` + `ssh v20usb` |
| 部署目录 | `~/live-capture`（Termux 私有目录） |
| Python | `.venv` 虚拟环境（Flask 3 + waitress + websockets 17.4）；系统 python3 **没有**这些依赖 |
| 数据 | `~/live-capture/data/`（capture.db、rooms.json、config.json、service.log、service.pid） |

## 常规发布流程

```bash
# 1. 推文件（host 上执行；注意多源 scp 会把 web/dist 落成 ~/live-capture/dist）
cd /d/HibaneHinome/live-capture
scp -q capture.py v20:~/live-capture/capture.py          # 单文件这样推
ssh v20 "rm -rf ~/live-capture/web/dist && mkdir -p ~/live-capture/web/dist"
scp -q -r web/dist/. v20:~/live-capture/web/dist/         # 前端产物整体推

# 2. 重启（自愈脚本同款命令）
ssh v20 "cd ~/live-capture && kill \$(cat data/service.pid) 2>/dev/null; sleep 2; \
  nohup .venv/bin/python run.py > data/service.log 2>&1 & echo \$! > data/service.pid; sleep 6; \
  curl -s -m 6 http://127.0.0.1:8765/api/health; tail -5 data/service.log"
```

- 服务重启后 `auto_start=true` 会**自动恢复**开着「直播数据」的采集器（日志出现「自动开始采集」）。
- 访问口令存手机 `data/config.json`，不要写进代码或文档。

## 开机自愈（灭屏/重启后）

- 机制在 `~/.bashrc`（仓库备份 `termux-boot/bashrc-snippet.sh`）：幂等自检 wake-lock / sshd / 采集服务，缺哪个补哪个。**每次打开 Termux 即恢复**，重启手机后打开一次 Termux 就够。
- 自愈脚本用 `.venv/bin/python` 启动——改启动方式时必须同步改它和仓库备份。
- Termux:Boot 装不上（手机 Termux 是第三方重签名，`INSTALL_FAILED_SHARED_USER_INCOMPATIBLE`），不要重试。
- 灭屏实测：Doze 白名单 + EXEMPTED bucket + 前台服务 + wake-lock 四道防线齐备，`force-idle` 深度休眠下服务存活。华为「应用启动管理」列表里没有 Termux，管不到它。
- sshd 不会自启（Termux 被杀后）：手机上敲 `sshd`，或 adb 触发 Termux 输入（见 pitfalls.md）。

## 备份与数据安全

- 数据全在 `~/live-capture/data/`，备份：`ssh v20 "cd ~/live-capture && tar czf /sdcard/live-capture-backup-$(date +%m%d).tgz data"`，再 `adb pull` 或 scp 回来。
- 删除数据只有两个入口：场次删除接口、`/api/clear`。历史导入可重复执行（内容哈希去重）。

## 常见操作

| 场景 | 操作 |
|---|---|
| 看服务状态 | `curl http://192.168.10.107:8765/api/health`（本机即可，免口令） |
| 看实时日志 | `ssh v20 "tail -f ~/live-capture/data/service.log"` |
| 手动恢复某采集器 | `POST /api/capture/start {"room_id": N}`（带口令 header） |
| 删幻影场次 | 网页采集器页每条场次右侧 ✕，或 `POST /api/session/delete` |
| 导入历史弹幕 | `ssh v20 "cd ~/live-capture && .venv/bin/python import_history.py --src /sdcard/... --gap 30"` |
| 查线程/内存 | `ssh v20 "cat /proc/$(cat ~/live-capture/data/service.pid)/status \| grep -E 'Threads|VmRSS'"` |
