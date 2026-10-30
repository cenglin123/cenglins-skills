#!/usr/bin/env bash
# srv_project.sh — 共享部署服务器上的项目账号与 SSH 授权管理。以 root 运行（sudo）。
#
# 每个项目一个 Linux 账号，家目录即 /srv/<项目>（权限 750），账号只能写自己的目录，
# 无 sudo、无密码登录、无端口转发；可以用 systemctl --user 管理自己的服务（已开 linger）。
# 授权公钥集中存放在 root 所有的 /etc/ssh/srv-keys/<账号>，项目账号自己改不了。
#
#   install                                   一次性：建组、公钥目录、sshd 规则
#   create <项目> --port <端口> [--contact <联系人>]
#   grant  <项目> <公钥文件|-> --label <标签> [--from <来源地址>]
#   revoke <项目> <标签|SHA256:指纹>
#   disable <项目>                            停用：禁止登录、停掉该账号全部进程与服务
#   enable  <项目>                            启用：恢复登录，已 enable 的用户服务随之启动
#   list
set -euo pipefail

GROUP=srvproj
SRV_ROOT=/srv
KEYS_DIR=/etc/ssh/srv-keys
SSHD_DROPIN=/etc/ssh/sshd_config.d/60-srv-projects.conf
REGISTRY=$SRV_ROOT/PROJECTS.tsv

die() { echo "错误：$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "需要 root：sudo $0 $*"; }
need_installed() { [ -f "$SSHD_DROPIN" ] && getent group "$GROUP" >/dev/null || die "尚未 install"; }

valid_name() {
    [[ "$1" =~ ^[a-z][a-z0-9_-]{1,31}$ ]] || die "项目名只能用小写字母、数字、- 和 _，以字母开头，2–32 个字符：$1"
}

is_project() { id -nG "$1" 2>/dev/null | tr ' ' '\n' | grep -qx "$GROUP"; }

reload_sshd() {
    sshd -t || die "sshd 配置检查失败，未重载"
    systemctl try-reload-or-restart ssh 2>/dev/null || systemctl try-reload-or-restart sshd
}

cmd_install() {
    groupadd -f "$GROUP"
    install -d -o root -g root -m 755 "$KEYS_DIR"
    cat >"$SSHD_DROPIN" <<EOF
# 由 srv_project.sh 管理：/srv 项目账号只认 root 管理的公钥，禁止密码与各类转发
Match Group $GROUP
    AuthorizedKeysFile $KEYS_DIR/%u
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    PermitTunnel no
    X11Forwarding no
EOF
    chmod 644 "$SSHD_DROPIN"
    [ -f "$REGISTRY" ] || printf '# 项目\t端口\t联系人\t创建时间\n' >"$REGISTRY"
    chmod 644 "$REGISTRY"
    reload_sshd
    echo "已安装：组 $GROUP、$KEYS_DIR、$SSHD_DROPIN、$REGISTRY"
    check_srv_perms
}

check_srv_perms() {
    local d
    for d in "$SRV_ROOT"/*/; do
        [ -d "$d" ] || continue
        if [ -n "$(find "$d" -maxdepth 0 -perm -o=r)" ]; then
            echo "警告：$d 对其他账号可读，项目账号能看到其中内容（属主可执行 chmod o-rwx $d）"
        fi
    done
}

port_in_use() {
    ss -Hltn "sport = :$1" | grep -q . || awk -F'\t' -v p="$1" '$2 == p {f=1} END {exit !f}' "$REGISTRY"
}

cmd_create() {
    local name=${1:-} port="" contact=""
    [ -n "$name" ] || die "用法：create <项目> --port <端口> [--contact <联系人>]"
    shift
    while [ $# -gt 0 ]; do
        case $1 in
            --port) port=${2:-}; shift 2 ;;
            --contact) contact=${2:-}; shift 2 ;;
            *) die "未知参数：$1" ;;
        esac
    done
    valid_name "$name"
    need_installed
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || die "--port 须为 1024–65535"
    id "$name" >/dev/null 2>&1 && die "账号 $name 已存在"
    [ -e "$SRV_ROOT/$name" ] && die "$SRV_ROOT/$name 已存在，不接管已有目录"
    port_in_use "$port" && die "端口 $port 已被占用或已登记"
    [[ "$contact" != *$'\t'* && "$contact" != *$'\n'* ]] || die "--contact 不能含制表符或换行"

    useradd --create-home --home-dir "$SRV_ROOT/$name" --shell /bin/bash --user-group --groups "$GROUP" "$name"
    chmod 750 "$SRV_ROOT/$name"
    install -d -o "$name" -g "$name" -m 750 "$SRV_ROOT/$name"/{repo,config,data,backups}
    install -o root -g root -m 644 /dev/null "$KEYS_DIR/$name"
    loginctl enable-linger "$name"
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        usermod -aG "$name" "$SUDO_USER"
    fi
    printf '%s\t%s\t%s\t%s\n' "$name" "$port" "$contact" "$(date '+%F %T')" >>"$REGISTRY"
    echo "已创建项目账号 $name：目录 $SRV_ROOT/$name/{repo,config,data,backups}，端口 $port；尚无授权公钥，用 grant 添加"
}

cmd_grant() {
    local name=${1:-} src=${2:-} label="" from="" fp line
    [ -n "$name" ] && [ -n "$src" ] || die "用法：grant <项目> <公钥文件|-> --label <标签> [--from <来源地址>]"
    shift 2
    while [ $# -gt 0 ]; do
        case $1 in
            --label) label=${2:-}; shift 2 ;;
            --from) from=${2:-}; shift 2 ;;
            *) die "未知参数：$1" ;;
        esac
    done
    valid_name "$name"
    need_installed
    is_project "$name" || die "$name 不是 $GROUP 项目账号"
    [[ "$label" =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || die "--label 必填，只能用字母、数字和 . _ @ -"
    [ -z "$from" ] || [[ "$from" =~ ^[0-9A-Za-z.:/*?,!-]+$ ]] || die "--from 格式不对：$from"

    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    if [ "$src" = - ]; then cat >"$tmp"; else cat -- "$src" >"$tmp"; fi
    grep -q "PRIVATE KEY" "$tmp" && die "给的是私钥。只接受公钥（.pub），私钥请对方自己保管"
    [ "$(grep -cv '^[[:space:]]*$' "$tmp")" -eq 1 ] || die "公钥文件应当只有一行"
    fp=$(ssh-keygen -lf "$tmp" | awk '{print $2}') || die "不是有效的 SSH 公钥"
    grep -q "^ssh-\|^ecdsa-\|^sk-" "$tmp" || die "公钥行不能带选项前缀，只要「类型 密钥 [注释]」"

    while read -r fpl lbl; do
        [ "$fpl" = "$fp" ] && die "这把公钥已授权（标签 $lbl）"
        [ "$lbl" = "$label" ] && die "标签 $label 已被另一把公钥使用"
    done < <(keys_of "$name")

    line="restrict,pty${from:+,from=\"$from\"} $(awk '{print $1, $2}' "$tmp") $label"
    printf '%s\n' "$line" >>"$KEYS_DIR/$name"
    echo "已授权 $name ← $label（$fp）${from:+，仅限来源 $from}"
}

# 输出「指纹 标签」，每把公钥一行
keys_of() {
    local f=$KEYS_DIR/$1 line
    [ -s "$f" ] || return 0
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        printf '%s %s\n' "$(ssh-keygen -lf - <<<"$line" | awk '{print $2}')" "${line##* }"
    done <"$f"
}

cmd_revoke() {
    local name=${1:-} key=${2:-} f tmp n=0 line
    [ -n "$name" ] && [ -n "$key" ] || die "用法：revoke <项目> <标签|SHA256:指纹>"
    valid_name "$name"
    need_installed
    is_project "$name" || die "$name 不是 $GROUP 项目账号"
    f=$KEYS_DIR/$name
    tmp=$(mktemp "$KEYS_DIR/.$name.XXXXXX")
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if [ "${line##* }" = "$key" ] || [ "$(ssh-keygen -lf - <<<"$line" | awk '{print $2}')" = "$key" ]; then
            n=$((n + 1))
        else
            printf '%s\n' "$line" >>"$tmp"
        fi
    done <"$f"
    if [ "$n" -eq 0 ]; then rm -f "$tmp"; die "$name 下没有标签或指纹为 $key 的公钥"; fi
    chown root:root "$tmp"; chmod 644 "$tmp"; mv -f "$tmp" "$f"
    echo "已吊销 $name 的 $n 把公钥（$key）。已建立的 SSH 会话不受影响，要立即断开：pkill -u $name -x sshd"
}

is_disabled() {
    [ "$(getent shadow "$1" | cut -d: -f8)" = 1 ]
}

cmd_disable() {
    local name=${1:-}
    [ -n "$name" ] || die "用法：disable <项目>"
    valid_name "$name"
    need_installed
    is_project "$name" || die "$name 不是 $GROUP 项目账号"
    usermod --expiredate 1 "$name"
    loginctl disable-linger "$name"
    systemctl stop "user@$(id -u "$name").service" 2>/dev/null || true
    pkill -KILL -u "$name" 2>/dev/null || true
    echo "已停用 $name：账号已过期（SSH 登录被拒），该账号的进程和用户服务已全部停止；数据未动"
}

cmd_enable() {
    local name=${1:-}
    [ -n "$name" ] || die "用法：enable <项目>"
    valid_name "$name"
    need_installed
    is_project "$name" || die "$name 不是 $GROUP 项目账号"
    usermod --expiredate '' "$name"
    loginctl enable-linger "$name"
    echo "已启用 $name：可以登录，已 enable 的用户服务随 linger 启动"
}

cmd_list() {
    need_installed
    local name port contact created
    while IFS=$'\t' read -r name port contact created; do
        [[ "$name" == \#* || -z "$name" ]] && continue
        echo "$name  端口 $port  联系人 ${contact:--}  创建 $created"
        if ! id "$name" >/dev/null 2>&1; then echo "  （账号已不存在）"; continue; fi
        if is_disabled "$name"; then echo "  状态：停用"; else echo "  状态：启用"; fi
        keys_of "$name" | sed 's/^/  公钥 /'
    done <"$REGISTRY"
    check_srv_perms
}

need_root "$@"
case ${1:-} in
    install) shift; cmd_install "$@" ;;
    create) shift; cmd_create "$@" ;;
    grant) shift; cmd_grant "$@" ;;
    revoke) shift; cmd_revoke "$@" ;;
    disable) shift; cmd_disable "$@" ;;
    enable) shift; cmd_enable "$@" ;;
    list) shift; cmd_list "$@" ;;
    *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
