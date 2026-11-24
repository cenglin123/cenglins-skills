#!/usr/bin/env bash
# install_panel.sh — 安装 /srv 项目管理面板（以 root 运行，可重复执行）。
#   sudo bash install_panel.sh [--lan-ip <IP>] [--allow-from <CIDR>]... [--enroll-only | --full]
#     --enroll-only  登记模式：只开放 POST /enroll，管理页一律 404（有知识库后台管理项目的服务器用）
#     --full         完整模式：恢复管理页；两者都不给则保持原有模式（首次安装默认完整模式）
#   sudo bash install_panel.sh --reset-admin     忘记密码：重置 admin 的初始密码
# 前提：已安装 srv-project（/usr/local/sbin/srv-project）并执行过 install。
set -euo pipefail

LIB_DIR=/usr/local/lib/srv-panel
CONF_DIR=/etc/srv-panel
TOKEN_FILE=$CONF_DIR/token
USERS_FILE=$CONF_DIR/users.json
PW_FILE=$CONF_DIR/initial-password
CERT=$CONF_DIR/tls.crt
KEY=$CONF_DIR/tls.key
UNIT=/etc/systemd/system/srv-panel.service
PORT=9300
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

die() { echo "错误：$*" >&2; exit 1; }

LAN_IP=""
RESET=0
MODE_FLAG=""
ALLOW_FROM=()
while [ $# -gt 0 ]; do
    case $1 in
        --lan-ip) LAN_IP=${2:-}; [ -n "$LAN_IP" ] || die "--lan-ip 需要一个 IP"; shift 2 ;;
        --allow-from) [ -n "${2:-}" ] || die "--allow-from 需要一个网段（CIDR）"; ALLOW_FROM+=("$2"); shift 2 ;;
        --reset-admin) RESET=1; shift ;;
        --enroll-only) [ -z "$MODE_FLAG" ] || [ "$MODE_FLAG" = --enroll-only ] || die "--enroll-only 与 --full 不能同时给"; MODE_FLAG=--enroll-only; shift ;;
        --full) [ -z "$MODE_FLAG" ] || [ "$MODE_FLAG" = --full ] || die "--enroll-only 与 --full 不能同时给"; MODE_FLAG=--full; shift ;;
        *) die "未知参数：$1" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "需要 root：sudo bash $0"
command -v python3 >/dev/null || die "没有 python3"
[ -f "$HERE/srv_panel.py" ] || die "找不到 $HERE/srv_panel.py"

# ── 忘记密码：重置 admin（不存在则清空用户库重建），不动其他安装内容
if [ "$RESET" -eq 1 ]; then
    [ -f "$LIB_DIR/srv_panel.py" ] || die "面板尚未安装，请先不带 --reset-admin 运行本脚本"
    python3 "$LIB_DIR/srv_panel.py" --reset-admin
    systemctl restart srv-panel.service 2>/dev/null || true   # 重启即注销所有已登录会话
    echo "新的初始密码已写入 $PW_FILE（root 可读），请用用户名 admin 登录，登录后必须改密。"
    exit 0
fi

command -v openssl >/dev/null || die "没有 openssl（apt install openssl）"
[ -x /usr/local/sbin/srv-project ] || echo "提示：尚未安装 /usr/local/sbin/srv-project，面板可以启动，但变更操作会失败" >&2

# ── 局域网 IP：参数优先，否则取默认路由出口的 IPv4
if [ -z "$LAN_IP" ]; then
    LAN_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')
    [ -n "$LAN_IP" ] || die "取不到默认路由网卡的 IPv4，请用 --lan-ip <IP> 指定"
    echo "自动取默认路由网卡的 IPv4：$LAN_IP（不对请重跑并加 --lan-ip <IP>）"
fi
SAN=$(python3 "$HERE/srv_panel.py" --print-san "$LAN_IP") || die "--lan-ip 无效：$LAN_IP"
for c in ${ALLOW_FROM[@]+"${ALLOW_FROM[@]}"}; do
    python3 "$HERE/srv_panel.py" --check-cidr "$c" || exit 1
done

install -d -o root -g root -m 755 "$LIB_DIR"
install -o root -g root -m 755 "$HERE/srv_panel.py" "$LIB_DIR/srv_panel.py"
install -d -o root -g root -m 700 "$CONF_DIR"

# ── 旧版令牌登录已取消
if [ -e "$TOKEN_FILE" ]; then
    rm -f "$TOKEN_FILE"
    echo "已删除旧的令牌文件 $TOKEN_FILE（现在用账号密码登录）"
fi

# ── 自签证书：缺失、SAN 不含当前 IP、或 30 天内到期时重新生成
need_cert=0
if [ ! -s "$CERT" ] || [ ! -s "$KEY" ]; then
    need_cert=1
elif ! openssl x509 -in "$CERT" -noout -text | grep -A1 'Subject Alternative Name' | grep -q "IP Address:$LAN_IP\b"; then
    need_cert=1
elif ! openssl x509 -in "$CERT" -noout -checkend $((30 * 86400)) >/dev/null; then
    need_cert=1
fi
if [ "$need_cert" -eq 1 ]; then
    (
        umask 077
        openssl req -x509 -newkey rsa:2048 -nodes -days 1095 -subj "/CN=srv-panel" \
            -addext "subjectAltName=$SAN" -addext "keyUsage=digitalSignature,keyEncipherment" \
            -addext "extendedKeyUsage=serverAuth" -keyout "$KEY" -out "$CERT" 2>/dev/null
    ) || die "生成证书失败"
    echo "已生成自签证书（3 年，SAN：$SAN）"
else
    echo "沿用已有证书 $CERT"
fi
chown root:root "$CERT" "$KEY"
chmod 644 "$CERT"
chmod 600 "$KEY"

# ── 面板配置：服务与 srv-project 的 invite/api（CLI）共用，保证邀请里的地址、端口、证书指纹与服务一致。
#    模式（enroll_only）只在给了 --enroll-only / --full 时改变，其余重复安装保留原值。
MODE=$(python3 "$LIB_DIR/srv_panel.py" --write-panel-conf --lan-ip "$LAN_IP" --port "$PORT" --tls-cert "$CERT" $MODE_FLAG | tail -n 1)
case $MODE in enroll_only=true) ENROLL_ONLY=1 ;; enroll_only=false) ENROLL_ONLY=0 ;; *) die "写入 panel.conf 失败" ;; esac

# ── 用户库：完整模式下不存在时建 admin 并写初始密码文件（不打印密码）；登记模式不需要账号体系
if [ "$ENROLL_ONLY" -eq 0 ]; then
    if [ ! -s "$USERS_FILE" ]; then
        python3 "$LIB_DIR/srv_panel.py" --bootstrap
    fi
    chmod 600 "$USERS_FILE"
fi
# 邀请库：不存在时建空库；重复安装保留已有邀请
[ -s "$CONF_DIR/invites.json" ] || python3 - "$CONF_DIR/invites.json" <<'PYEND'
import os, sys
p = sys.argv[1]
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write('{"invites": {}, "version": 1}\n')
PYEND
[ ! -e "$CONF_DIR/invites.json" ] || chmod 600 "$CONF_DIR/invites.json"

EXTRA=""
for c in ${ALLOW_FROM[@]+"${ALLOW_FROM[@]}"}; do EXTRA="$EXTRA --allow-from $c"; done

cat >"$UNIT" <<UNITEOF
[Unit]
Description=/srv 项目管理面板（HTTPS，局域网 $LAN_IP 与本机回环）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 $LIB_DIR/srv_panel.py --bind $LAN_IP --bind 127.0.0.1 --port $PORT --allow-remote --allowed-host $LAN_IP --tls-cert $CERT --tls-key $KEY$EXTRA
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNITEOF
chmod 644 "$UNIT"

systemctl daemon-reload
systemctl enable srv-panel.service >/dev/null 2>&1
systemctl reset-failed srv-panel.service 2>/dev/null || true   # 连续重装触发 systemd 启动频率限制时也能重启
systemctl restart srv-panel.service
sleep 1
systemctl is-active --quiet srv-panel.service || die "srv-panel 启动失败：journalctl -u srv-panel -n 30"

echo "面板已启动：监听 $LAN_IP:$PORT（HTTPS）和 127.0.0.1:$PORT。"
if [ "$ENROLL_ONLY" -eq 1 ]; then
    echo "当前模式：登记模式（enroll-only）——只开放 POST /enroll，管理页一律 404；项目管理请用知识库后台【项目运维】或 srv-project。"
    echo "  恢复管理页：sudo bash $0 --full"
else
    echo "当前模式：完整模式——管理页和设备登记接口都开放。改为只开放登记接口：sudo bash $0 --enroll-only"
    echo "浏览器打开：https://$LAN_IP:$PORT"
    echo "  首次打开浏览器会警告自签证书，选择「高级 → 继续访问」确认一次即可。"
    if [ -s "$PW_FILE" ]; then
        echo "  首次登录：用户名 admin，初始密码在 $PW_FILE（sudo cat 查看），登录后必须改密；改密后该文件自动删除。"
    else
        echo "  已有用户库，用现有账号密码登录。忘记密码：sudo bash $0 --reset-admin"
    fi
fi
[ ${#ALLOW_FROM[@]} -eq 0 ] || echo "  只允许来源网段：${ALLOW_FROM[*]}（本机回环不受限）"
echo "设备邀请：面板「邀请」页，或 sudo srv-project invite <项目> --label <标签>"
echo "审计日志：/var/log/srv-panel.log"
