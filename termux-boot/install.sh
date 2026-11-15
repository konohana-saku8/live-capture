#!/data/data/com.termux/files/usr/bin/sh
# 把 termux-boot/ 下的脚本装到 ~/.termux/boot/。
# 用法（Termux 内）：sh /sdcard/live-capture/termux-boot/install.sh
# 生效前提：装了 Termux:Boot（com.termux.boot，F-Droid 版），否则目录存在也不会被执行。
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/.termux/boot"

mkdir -p "$DST"
for f in "$SRC"/10-sshd.sh "$SRC"/20-capture.sh; do
  [ -f "$f" ] || continue
  cp "$f" "$DST/"
  chmod 700 "$DST/$(basename "$f")"
  echo "installed $DST/$(basename "$f")"
done

echo "--- $DST ---"
ls -la "$DST"

if pm list packages 2>/dev/null | grep -q 'com.termux.boot'; then
  echo "Termux:Boot: 已安装"
else
  echo "Termux:Boot: 未安装 —— 脚本已就位，装完 app 后重启手机即生效"
fi
