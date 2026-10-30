#!/usr/bin/env bash
# install_panel.sh — 安装 /srv 项目管理面板（以 root 运行，可重复执行）。
#   sudo bash install_panel.sh
# 前提：已安装 srv-project（/usr/local/sbin/srv-project）并执行过 install。
set -euo pipefail

LIB_DIR=/usr/local/lib/srv-panel
CONF_DIR=/etc/srv-panel
TOKEN_FILE=$CONF_DIR/token
UNIT=/etc/systemd/system/srv-panel.service
PORT=9300
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

die() { echo "错误：$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "需要 root：sudo bash $0"
command -v python3 >/dev/null || die "没有 python3"
[ -f "$HERE/srv_panel.py" ] || die "找不到 $HERE/srv_panel.py"
[ -x /usr/local/sbin/srv-project ] || echo "提示：尚未安装 /usr/local/sbin/srv-project，面板可以启动，但变更操作会失败" >&2

install -d -o root -g root -m 755 "$LIB_DIR"
install -o root -g root -m 755 "$HERE/srv_panel.py" "$LIB_DIR/srv_panel.py"

install -d -o root -g root -m 700 "$CONF_DIR"
if [ ! -s "$TOKEN_FILE" ]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(24))' >"$TOKEN_FILE")
    echo "已生成登录令牌：$TOKEN_FILE"
else
    echo "沿用已有登录令牌：$TOKEN_FILE"
fi
chown root:root "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

cat >"$UNIT" <<UNITEOF
[Unit]
Description=/srv 项目管理面板（仅监听本机回环）
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 $LIB_DIR/srv_panel.py --bind 127.0.0.1 --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNITEOF
chmod 644 "$UNIT"

systemctl daemon-reload
systemctl enable srv-panel.service >/dev/null 2>&1
systemctl restart srv-panel.service
sleep 1
systemctl is-active --quiet srv-panel.service || die "srv-panel 启动失败：journalctl -u srv-panel -n 30"

cat <<MSG
面板已启动，只监听 127.0.0.1:$PORT。
访问方式：
  1. 管理员在自己电脑上执行：ssh -L $PORT:127.0.0.1:$PORT <服务器>
  2. 浏览器打开 http://127.0.0.1:$PORT
  3. 登录令牌在服务器上查看：sudo cat $TOKEN_FILE
审计日志：/var/log/srv-panel.log
MSG
