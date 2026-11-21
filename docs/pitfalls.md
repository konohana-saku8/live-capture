# 踩坑积累（按主题）

这里只收录「下次还会遇到」的坑。历史细节见 git log 与工作日志。

## B 站接口与协议

1. **幻影开播（两次真实事故）**
   - 形态一：状态接口瞬时误报 `live_status=1` 但 `live_start_time` 为空（"0000-00-00"）。真开播时 `live_start_time` 一定有值 → 判定条件必须是 `live_status==1 且 live_time>0`。
   - 形态二：`resolve_room` 抛异常时的「照连别饿死」兜底 → 状态是 0，连上即断，但补记已写出幻影「开播」。探状态失败必须等同没开播。
   - 识别特征：同秒内「已连接 → 补记开播 → 已下播断开」且库里没有配对下播事件。
2. **B 站不补发 LIVE**：连上弹幕服务器时拿不到开播事件，所以需要 `_note_live_start` 补记；补记与真实 LIVE 推送之间要 3 分钟去重，否则同一场被切成两半。
3. **SC 官方日译**：`SUPER_CHAT_MESSAGE_JPN` 只带译文与 sc_id，靠它回填 `message_trans`；报文顺序不定时先进 `_pending_trans` 暂存。
4. **CDN 防盗链**：`*.hdslb.com` 对 Referer 严格——本地/第三方域 Referer 一律 403；`https://live.bilibili.com/` 或**不带 Referer** 才 200。前端 `<img>` 必须带 `referrerpolicy="no-referrer"`，失败自动切 `/api/emote` 同源代理（白名单 + LRU 缓存）。
5. **账号资料**：nav API 依赖登录态，`x/web-interface/card?mid=` 公开可用（cookie 过期也能取头像昵称），作为主路径。

## Android / Termux

1. `/proc/stat` 对普通应用不可读（读到 0 字节），系统级 CPU 永远 0 → 退化到 `/proc/self/stat` 算本进程单核占用，返回值带 `scope` 标口径；两次采样间隔 <0.5 秒沿用上次值（否则轮询撞上手动刷新会把 1 tick 除成 1000%）。
2. adb 是 shell 用户，**进不了 Termux 私有目录**；执行命令走 `am start` 拉起 Termux → `input text`（空格用 `%s`）→ `keyevent 66`。部署结果要回写 `/sdcard/live-capture/DEPLOY_STATUS.txt` 才能从 adb 侧观察。
3. Termux 是第三方重签名版，**官方 Termux:Boot 装不上**（`INSTALL_FAILED_SHARED_USER_INCOMPATIBLE`），自启靠 `~/.bashrc` 幂等自愈。
4. 华为管家页面无法 `am start` 直达（`USE_COMPONENT` 权限挡），只能 `monkey` 拉主界面 + `input tap`（首页「应用启动管理」入口 ≈ (596,2075)，屏 1080x2400）。
5. 灭屏保活四件套（Doze 白名单 / standby bucket / 前台服务 / wake-lock）在本机已验证够用，`force-idle` 深度休眠不死。
6. 重装 Termux 后 UID 会变：`whoami` 重取用户名，公钥经 `/sdcard` 中转重推 `authorized_keys`，`~/.ssh/config` 同步改。

## 工具链

1. **Git Bash 的 MSYS 会把 `/sdcard/...` 改写成 Windows 路径**——adb push 一律用 PowerShell。
2. 沙箱内 PowerShell 没有 `ssh`，调 ssh 一律用 Bash 工具。
3. 本机沙箱外网走 `127.0.0.1:9094` 代理：curl 自动读 env；**headless Chrome 不读**，要显式 `--proxy-server`。验证防盗链图片优先本地 mock（`--host-resolver-rules=MAP *.hdslb.com 127.0.0.1` 让直连快速失败）。
4. headless Chrome 最小窗口宽约 500px；390px 截图右边被裁是工具假象，判断溢出要量 `scrollWidth`。
5. `Edit` 工具会把整档换行改 CRLF——shell 脚本 / HTML 提交前检查还原 LF。
6. 多源 `scp -r a b phone:~/x/` 的目录目标会平铺落位（`web/dist` 变 `~/x/dist`），推完核对哈希。
7. 常驻后台服务用 Bash 工具的 `run_in_background`，不要用 `&`（随 shell 退出被杀）。

## 前端

1. 门禁弹窗曾「每次进站先闪一下」：`gate.open` 初始 true + 先渲染后验证。正确顺序是**先隐藏界面 → 用本地 token 验证 → 通过直进，失败才探测并弹窗**。
2. 轮询覆盖用户输入：自动刷新回填表单/开关前必须检查「用户是否正在编辑或已改动」（快照比对 + dirty 标记），只回填切换时刻的数据。
3. `.modal` 的 `display:flex` 会压过 `[hidden]`——一律补 `[hidden]{display:none!important}`。
4. 白底立绘抠图按边缘连通域，不按颜色阈值（白花/眼白会被误删）——这是 live-archive 的经验，同属本工作区。
