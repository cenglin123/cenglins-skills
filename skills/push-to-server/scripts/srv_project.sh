#!/usr/bin/env bash
# srv_project.sh — 共享部署服务器上的项目账号与 SSH 授权管理。以 root 运行（sudo）。
#
# 每个项目一个 Linux 账号，家目录即 /srv/<项目>（权限 750），账号只能写自己的目录，
# 无 sudo、无密码登录、无端口转发；可以用 systemctl --user 管理自己的服务（已开 linger）。
# 授权公钥集中存放在 root 所有的 /etc/ssh/srv-keys/<账号>，项目账号自己改不了。
#
#   install [--delegate <用户>]...            一次性：建组、公钥目录、sshd 规则；--delegate 让该用户免密 sudo 本脚本
#   undelegate <用户>                         取消委托
#   create <项目> --port <端口> [--contact <联系人>]
#   grant  <项目> <公钥文件|-> --label <标签> [--from <来源地址>]
#   revoke <项目> <标签|SHA256:指纹>
#   disable <项目>                            停用：禁止登录、停掉该账号全部进程与服务
#   enable  <项目>                            启用：恢复登录，已 enable 的用户服务随之启动
#   info    <项目> [--host <服务器地址>]      打印可转发给申请方的接入信息（不含密钥内容与密码）
#   delete  <项目> [--purge]                  删除已停用的项目：删账号、公钥、登记；目录归档到 /srv/.archived（--purge 直接删除）
#   invite  <项目> --label <标签> [--hours N] [--from X]   生成一次性设备邀请（需先装管理面板）
#   invites <项目>                            列出该项目的邀请及状态
#   invite-revoke <项目> <邀请编号>           作废该项目下待使用的邀请（编号不属于该项目则报找不到）
#   list                                      人类可读的项目清单
#   JSON / 运维子命令（转调管理面板代码；失败时退出码非 0，原因写 stderr；项目不存在退出码 2）：
#   list --json                               轻量：只读登记表、shadow、公钥文件
#   status --json [<项目>]                    全量：监听、属主冲突、服务、git HEAD、磁盘、问题（指定项目时不含 global）
#   env --json <项目>                         以项目账号检测 git/node/npm/python3/docker 及 repo 声明
#   info --json <项目> [--host <地址>]        接入信息的结构化版本（含原文 text）
#   invites --json <项目>                     邀请列表（JSON）
#   logs <项目> <服务> [--lines N]            用户服务最近日志（纯文本，N 默认 100，1–1000）
#   svc <项目> <start|stop|restart> <服务>    启停用户服务
set -euo pipefail

GROUP=srvproj
SRV_ROOT=/srv
KEYS_DIR=/etc/ssh/srv-keys
SSHD_DROPIN=/etc/ssh/sshd_config.d/60-srv-projects.conf
REGISTRY=$SRV_ROOT/PROJECTS.tsv
SUDOERS=/etc/sudoers.d/srv-project
ARCHIVE_DIR=$SRV_ROOT/.archived
PANEL_PY=/usr/local/lib/srv-panel/srv_panel.py
SELF=/usr/local/sbin/srv-project

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

# ── sudo 委托：让指定的普通账号免密执行本脚本（且仅本脚本）
delegates_of() {
    [ -f "$SUDOERS" ] || return 0
    awk -v self="$SELF" '$2 == "ALL=(root)" && $3 == "NOPASSWD:" && $4 == self {print $1}' "$SUDOERS"
}

write_sudoers() {   # 参数：委托用户列表（可为空，空则删除文件）
    if [ $# -eq 0 ]; then rm -f "$SUDOERS"; return; fi
    command -v visudo >/dev/null || die "没有 visudo，先安装 sudo 包"
    local tmp u
    tmp=$(mktemp)
    {
        echo "# 由 srv_project.sh 管理：下列账号可免密执行 $SELF"
        for u in "$@"; do printf '%s ALL=(root) NOPASSWD: %s\n' "$u" "$SELF"; done
    } >"$tmp"
    if ! visudo -cf "$tmp" >/dev/null; then rm -f "$tmp"; die "sudoers 校验失败，未改动"; fi
    install -d -o root -g root -m 755 "$(dirname "$SUDOERS")"
    install -o root -g root -m 440 "$tmp" "$SUDOERS.new"
    mv -f "$SUDOERS.new" "$SUDOERS"
    rm -f "$tmp"
}

check_delegates() {
    local u
    for u in "$@"; do
        [[ "$u" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "用户名格式不对：$u"
        [ "$u" != root ] || die "root 不需要委托"
        id "$u" >/dev/null 2>&1 || die "用户 $u 不存在"
        ! is_project "$u" || die "$u 是 $GROUP 项目账号，不能作为委托用户"
    done
}

cmd_delegate() {
    local u cur=()
    check_delegates "$@"
    while IFS= read -r u; do [ -z "$u" ] || cur+=("$u"); done < <(delegates_of)
    for u in "$@"; do
        printf '%s\n' ${cur[@]+"${cur[@]}"} | grep -qx "$u" || cur+=("$u")
    done
    write_sudoers "${cur[@]}"
    echo "已委托（sudo 免密执行 $SELF）：${cur[*]}"
}

cmd_undelegate() {
    local u=${1:-} cur=() x
    [ -n "$u" ] || die "用法：undelegate <用户>"
    delegates_of | grep -qx "$u" || die "$u 不在委托名单里"
    while IFS= read -r x; do [ -z "$x" ] || [ "$x" = "$u" ] || cur+=("$x"); done < <(delegates_of)
    write_sudoers ${cur[@]+"${cur[@]}"}
    echo "已取消委托：$u"
}

cmd_install() {
    local delegates=()
    while [ $# -gt 0 ]; do
        case $1 in
            --delegate) [ -n "${2:-}" ] || die "--delegate 需要用户名"; delegates+=("$2"); shift 2 ;;
            *) die "未知参数：$1" ;;
        esac
    done
    check_delegates ${delegates[@]+"${delegates[@]}"}
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
    if [ ${#delegates[@]} -gt 0 ]; then cmd_delegate "${delegates[@]}"; fi
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
    if [ "$src" = - ]; then
        cat >"$tmp"
    elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        # 经 sudo 调用：只能读调用者自己读得到的文件，错误里不带任何文件内容
        runuser -u "$SUDO_USER" -- cat -- "$src" >"$tmp" 2>/dev/null || die "无法以 $SUDO_USER 身份读取 $src"
    else
        cat -- "$src" >"$tmp"
    fi
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
    if [ "${1:-}" = --json ]; then panel_api list; return; fi
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
    local d
    d=$(delegates_of | tr '\n' ' ')
    echo "委托用户（sudo 免密）：${d:-无}"
}

cmd_info() {
    local json=0 a rest=()
    for a in "$@"; do if [ "$a" = --json ]; then json=1; else rest+=("$a"); fi; done
    set -- ${rest[@]+"${rest[@]}"}
    if [ "$json" -eq 1 ]; then
        [ -n "${1:-}" ] || die "用法：info --json <项目> [--host <服务器地址>]"
        valid_name "$1"
        panel_api info "$@"
        return
    fi
    local name=${1:-} host="" port ssh_port acct_state f line from fp label
    [ -n "$name" ] || die "用法：info <项目> [--host <服务器地址>]"
    shift
    while [ $# -gt 0 ]; do
        case $1 in
            --host) host=${2:-}; shift 2 ;;
            *) die "未知参数：$1" ;;
        esac
    done
    valid_name "$name"
    need_installed
    is_project "$name" || die "$name 不是 $GROUP 项目账号"
    [[ -z "$host" || "$host" =~ ^[0-9A-Za-z._:-]+$ ]] || die "--host 格式不对：$host"
    if [ -z "$host" ]; then
        host=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}') || true
        host=${host:-"<服务器地址>"}
    fi
    ssh_port=$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}') || true
    ssh_port=${ssh_port:-22}
    port=$(awk -F'\t' -v n="$name" '$1 == n {print $2; exit}' "$REGISTRY")
    if is_disabled "$name"; then acct_state=停用; else acct_state=启用; fi
    echo "===== 接入信息：$name ====="
    echo "服务器地址：$host"
    echo "SSH 端口：$ssh_port"
    echo "SSH 账号：$name（仅密钥登录，无 sudo）"
    echo "项目目录：$SRV_ROOT/$name"
    echo "  repo/     代码目录（push-to-server 配置的 remote_repo_dir 填 $SRV_ROOT/$name/repo）"
    echo "  config/   配置与密钥，不进 Git"
    echo "  data/     数据库、上传文件等持久数据"
    echo "  backups/  数据备份"
    echo "登记端口：${port:--}"
    echo "状态：$acct_state"
    echo "已授权公钥："
    f=$KEYS_DIR/$name
    if [ -s "$f" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            fp=$(ssh-keygen -lf - <<<"$line" | awk '{print $2}')
            label=${line##* }
            from=$(sed -n 's/^.*from="\([^"]*\)".*$/\1/p' <<<"${line%% ssh-*}")
            echo "  标签 $label  指纹 $fp${from:+  仅限来源 $from}"
        done <"$f"
    else
        echo "  （暂无）"
    fi
    echo "请用 push-to-server 技能的 /srv 接入流程完成首次部署；应用初始密码由你的 agent 生成在 config/.env"
}

cmd_delete() {
    local name=${1:-} purge=0 uid n dest tmp
    [ -n "$name" ] || die "用法：delete <项目> [--purge]"
    shift
    while [ $# -gt 0 ]; do
        case $1 in
            --purge) purge=1; shift ;;
            *) die "未知参数：$1" ;;
        esac
    done
    valid_name "$name"
    need_installed
    is_project "$name" || die "$name 不是 $GROUP 项目账号"
    is_disabled "$name" || die "项目 $name 尚未停用，请先 disable"
    n=$(pgrep -c -u "$name" 2>/dev/null || true)
    [ "${n:-0}" -eq 0 ] || die "账号 $name 仍有 $n 个进程在运行，拒绝删除"
    [ "$(getent passwd "$name" | cut -d: -f6)" = "$SRV_ROOT/$name" ] || die "$name 的家目录不是 $SRV_ROOT/$name，拒绝删除"
    uid=$(id -u "$name")

    loginctl disable-linger "$name" 2>/dev/null || true
    rm -f "/var/lib/systemd/linger/$name"
    systemctl stop "user@$uid.service" 2>/dev/null || true
    userdel "$name" 2>/dev/null || die "userdel $name 失败，未做其他改动"
    if getent group "$name" >/dev/null; then groupdel "$name" 2>/dev/null || true; fi
    rm -f "$KEYS_DIR/$name"
    tmp=$(mktemp)
    awk -F'\t' -v n="$name" '$1 != n' "$REGISTRY" >"$tmp"
    cat "$tmp" >"$REGISTRY"
    rm -f "$tmp"

    if [ "$purge" -eq 1 ]; then
        rm -rf --one-file-system -- "${SRV_ROOT:?}/$name"
        echo "已删除项目 $name：账号、公钥、登记表条目和目录 $SRV_ROOT/$name 均已清除"
    else
        install -d -o root -g root -m 700 "$ARCHIVE_DIR"
        dest=$ARCHIVE_DIR/$name-$(date +%Y%m%d-%H%M%S)
        mv -- "$SRV_ROOT/$name" "$dest"
        chown -R root:root "$dest"
        echo "已删除项目 $name：账号、公钥、登记表条目已清除；目录已归档到 $dest（属主 root，需要时手动处理）"
    fi
}

# 邀请由管理面板的代码管理（同一份邀请库），这里只转调
panel_invite() {
    [ -f "$PANEL_PY" ] || die "请先安装管理面板（panel/install_panel.sh）"
    python3 "$PANEL_PY" invite "$@"
}

cmd_invite() {
    [ -n "${1:-}" ] || die "用法：invite <项目> --label <标签> [--hours N] [--from X]"
    local project=$1
    shift
    valid_name "$project"
    need_installed
    is_project "$project" || die "$project 不是 $GROUP 项目账号"
    panel_invite create "$project" "$@"
}

cmd_invites() {
    local json=0 a rest=()
    for a in "$@"; do if [ "$a" = --json ]; then json=1; else rest+=("$a"); fi; done
    set -- ${rest[@]+"${rest[@]}"}
    [ -n "${1:-}" ] || die "用法：invites [--json] <项目>"
    valid_name "$1"
    if [ "$json" -eq 1 ]; then panel_api invites "$1"; else panel_invite list --project "$1"; fi
}

cmd_invite_revoke() {
    [ -n "${1:-}" ] && [ -n "${2:-}" ] && [ $# -eq 2 ] || die "用法：invite-revoke <项目> <邀请编号>"
    valid_name "$1"
    panel_invite revoke "$1" "$2"
}

# JSON / 运维子命令：同样转调管理面板代码
panel_api() {
    [ -f "$PANEL_PY" ] || die "请先安装管理面板（panel/install_panel.sh）"
    python3 "$PANEL_PY" api "$@"
}

valid_unit() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,99}\.service$ ]] || die "服务名格式不对：$1"
}

cmd_status() {
    [ "${1:-}" = --json ] || die "用法：status --json [<项目>]"
    shift
    if [ -n "${1:-}" ]; then valid_name "$1"; panel_api status "$1"; else panel_api status; fi
}

cmd_env() {
    [ "${1:-}" = --json ] && [ -n "${2:-}" ] || die "用法：env --json <项目>"
    valid_name "$2"
    panel_api env "$2"
}

cmd_logs() {
    local project=${1:-} unit=${2:-} lines=100
    [ -n "$project" ] && [ -n "$unit" ] || die "用法：logs <项目> <服务> [--lines N]"
    shift 2
    while [ $# -gt 0 ]; do
        case $1 in
            --lines) lines=${2:-}; shift 2 ;;
            *) die "未知参数：$1" ;;
        esac
    done
    valid_name "$project"
    valid_unit "$unit"
    [[ "$lines" =~ ^[0-9]+$ ]] && [ "$lines" -ge 1 ] && [ "$lines" -le 1000 ] || die "--lines 须为 1–1000"
    panel_api logs "$project" "$unit" --lines "$lines"
}

cmd_svc() {
    [ $# -eq 3 ] || die "用法：svc <项目> <start|stop|restart> <服务>"
    valid_name "$1"
    case $2 in start | stop | restart) ;; *) die "操作须为 start、stop 或 restart：$2" ;; esac
    valid_unit "$3"
    panel_api svc "$1" "$2" "$3"
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
    info) shift; cmd_info "$@" ;;
    delete) shift; cmd_delete "$@" ;;
    undelegate) shift; cmd_undelegate "$@" ;;
    invite) shift; cmd_invite "$@" ;;
    invites) shift; cmd_invites "$@" ;;
    invite-revoke) shift; cmd_invite_revoke "$@" ;;
    status) shift; cmd_status "$@" ;;
    env) shift; cmd_env "$@" ;;
    logs) shift; cmd_logs "$@" ;;
    svc) shift; cmd_svc "$@" ;;
    *) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
