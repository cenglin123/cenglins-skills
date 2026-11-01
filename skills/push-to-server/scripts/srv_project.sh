#!/usr/bin/env bash
# srv_project.sh — 共享部署服务器上的项目账号与 SSH 授权管理。以 root 运行（sudo）。
#
# 每个项目一个 Linux 账号，家目录即 /srv/<项目>（权限 750），账号只能写自己的目录，
# 无 sudo、无密码登录、无端口转发；可以用 systemctl --user 管理自己的服务（已开 linger）。
# 授权公钥集中存放在 root 所有的 /etc/ssh/srv-keys/<账号>，项目账号自己改不了。
#
#   install [--delegate <用户>]...            一次性：建组、公钥目录、sshd 规则；--delegate 让该用户免密 sudo 本脚本
#   undelegate <用户>                         取消委托
#   create <项目> --port <端口> [--contact <联系人>] [--takeover <外部服务>]   --takeover：从已登记且可迁入的外部服务迁入（端口须等于它的端口）
#   grant  <项目> <公钥文件|-> --label <标签> [--from <来源地址>]
#   revoke <项目> <标签|SHA256:指纹>
#   disable <项目>                            停用：禁止登录、停掉该账号全部进程与服务
#   enable  <项目>                            启用：恢复登录，已 enable 的用户服务随之启动
#   info    <项目> [--host <服务器地址>]      打印可转发给申请方的接入信息（不含密钥内容与密码）
#   delete  <项目> [--purge]                  删除已停用的项目：删账号、公钥、登记；目录归档到 /srv/.archived（--purge 直接删除）
#   invite  <项目> --label <标签> [--hours N] [--from X]   生成一次性设备邀请（需先装管理面板）
#   invites <项目>                            列出该项目的邀请及状态
#   invite-revoke <项目> <邀请编号>           作废该项目下待使用的邀请（编号不属于该项目则报找不到）
#   list                                      人类可读的项目清单（不含注释；注释只进 --json 与知识库后台）
#   note <项目> --set=<文本> | --set=- | --clear   设置/清除项目注释（≤200 字符，单行）；--set=- 从 stdin 读正文，推荐
#   JSON / 运维子命令（转调管理面板代码；失败时退出码非 0，原因写 stderr；项目不存在退出码 2）：
#   list --json                               轻量：只读登记表、shadow、公钥文件
#   status --json [<项目>]                    全量：监听、属主冲突、服务、git HEAD、磁盘、问题（指定项目时不含 global）
#   env --json <项目>                         以项目账号检测 git/node/npm/python3/docker 及 repo 声明
#   info --json <项目> [--host <地址>]        接入信息的结构化版本（含原文 text）
#   invites --json <项目>                     邀请列表（JSON）
#   logs <项目> <服务> [--lines N]            用户服务最近日志（纯文本，N 默认 100，1–1000）
#   svc <项目> <start|stop|restart> <服务>    启停用户服务
#   外部服务（只读登记存量服务；读写与校验全在管理面板代码里，本脚本只透传）：
#   external add <名称> --port N --owner <账号> [--unit user:X.service|system:X.service] [--desc 说明] [--migrate]
#   external remove <名称> [--cancel-migration]   迁入中默认拒绝；已切换可直接删；--cancel-migration 放弃迁移并清项目来源
#   external list --json                      登记的外部服务及监听、迁入状态
#   takeover-copy <项目> <源目录> [--to data|config]   把迁入项目的旧数据复制进 /srv/<项目>/<data|config>/（调用者须是旧服务属主或 root）
set -euo pipefail

GROUP=srvproj
SRV_ROOT=/srv
KEYS_DIR=/etc/ssh/srv-keys
SSHD_DROPIN=/etc/ssh/sshd_config.d/60-srv-projects.conf
REGISTRY=$SRV_ROOT/PROJECTS.tsv
REGISTRY_LOCK=$SRV_ROOT/.PROJECTS.lock   # 追加/删除/面板改写登记表时共用的 flock 文件
EXTERNAL=$SRV_ROOT/EXTERNAL.tsv
SUDOERS=/etc/sudoers.d/srv-project
ARCHIVE_DIR=$SRV_ROOT/.archived
PANEL_PY=/usr/local/lib/srv-panel/srv_panel.py
SELF=/usr/local/sbin/srv-project

die() { echo "错误：$*" >&2; exit 1; }
die_usage() { echo "错误：$*" >&2; exit 2; }   # 用法/互斥错误退出码 2（与面板 argparse 一致）；校验失败才是 1
need_root() { [ "$(id -u)" -eq 0 ] || die "需要 root：sudo $0 $*"; }
need_installed() { [ -f "$SSHD_DROPIN" ] && getent group "$GROUP" >/dev/null || die "尚未 install"; }

valid_name() {
    [[ "$1" =~ ^[a-z][a-z0-9_-]{1,31}$ ]] || die "项目名只能用小写字母、数字、- 和 _，以字母开头，2–32 个字符：$1"
}

is_project() { id -nG "$1" 2>/dev/null | tr ' ' '\n' | grep -qx "$GROUP"; }

registry_has() {   # 登记表里有这个名称的行（账号可能不存在：create 在 register 之后、useradd 之前被中断）
    [ -f "$REGISTRY" ] && awk -F'\t' -v n="$1" '$1 == n {f=1} END {exit !f}' "$REGISTRY"
}

registry_remove() {
    local name=$1 tmp
    [ -f "$REGISTRY" ] || return 0
    tmp=$(mktemp)
    ( flock 9; awk -F'\t' -v n="$name" '$1 != n' "$REGISTRY" >"$tmp"; cat "$tmp" >"$REGISTRY" ) 9>"$REGISTRY_LOCK"
    rm -f "$tmp"
}

release_external_target() {   # 清掉外部服务上指向该项目的迁入目标
    local name=$1
    if [ -f "$EXTERNAL" ]; then
        if [ -f "$PANEL_PY" ] && python3 "$PANEL_PY" api external release "$name"; then :; else
            echo "警告：未能清理 $EXTERNAL 里指向 $name 的迁入标记，可用 srv-project external remove <外部服务> --cancel-migration 处理" >&2
        fi
    fi
}

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
    [ -f "$REGISTRY" ] || printf '# 项目\t端口\t联系人\t创建时间\t来源\t注释\n' >"$REGISTRY"
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

port_registered() {   # 端口已登记在项目表里（任何情况下都不豁免）
    awk -F'\t' -v p="$1" '$1 !~ /^#/ && $2 + 0 == p + 0 {f=1} END {exit !f}' "$REGISTRY"
}

port_in_use() {
    ss -Hltn "sport = :$1" | grep -q . || port_registered "$1"
}

# create：登记行由面板 register 在两把锁内复查唯一性后追加（检查与落盘同一临界区，不给并发留窗口）。
# register 之后任一步失败，trap 回滚登记行与迁入标记，并给出修复命令。本脚本持 flock 时不再调面板（面板会取同一把锁，会死锁）。
TK_EXT="" TK_PROJECT="" TK_REGISTERED=""
create_rollback() {
    local rc=$?
    trap - EXIT
    if [ "$rc" -ne 0 ]; then
        if [ -n "$TK_REGISTERED" ]; then
            if python3 "$PANEL_PY" api external unregister "$TK_PROJECT"; then
                echo "已回滚 $REGISTRY 里 $TK_PROJECT 的登记行" >&2
            else
                echo "错误：登记行回滚失败；请手动从 $REGISTRY 删掉 $TK_PROJECT 行" >&2
            fi
        fi
        if [ -n "$TK_EXT" ]; then
            if python3 "$PANEL_PY" api external takeover-abort "$TK_EXT" --project "$TK_PROJECT"; then
                echo "已回滚外部服务 $TK_EXT 的迁入标记" >&2
            else
                echo "错误：迁入标记回滚失败；请手动执行：srv-project external remove $TK_EXT --cancel-migration（会同时删除该登记，需要的话再重新 external add）" >&2
            fi
        fi
    fi
    exit "$rc"
}

cmd_create() {
    local name=${1:-} port="" contact="" takeover="" src=""
    [ -n "$name" ] || die "用法：create <项目> --port <端口> [--contact <联系人>] [--takeover <外部服务>]"
    shift
    while [ $# -gt 0 ]; do
        case $1 in
            --port) port=${2:-}; shift 2 ;;
            --contact) contact=${2:-}; shift 2 ;;
            --takeover) takeover=${2:-}; shift 2 ;;
            *) die "未知参数：$1" ;;
        esac
    done
    valid_name "$name"
    need_installed
    [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || die "--port 须为 1024–65535 的整数（不带前导 0）"
    id "$name" >/dev/null 2>&1 && die "账号 $name 已存在"
    [ -e "$SRV_ROOT/$name" ] && die "$SRV_ROOT/$name 已存在，不接管已有目录"
    [[ "$contact" != *$'\t'* && "$contact" != *$'\n'* ]] || die "--contact 不能含制表符或换行"
    # 下面是建议性 fail-fast（不持锁，读了也拦不住并发）；权威的唯一性核对在 register 的锁内复查里
    if [ -n "$takeover" ]; then
        valid_name "$takeover"
        # 迁入只豁免外部服务那一个端口；项目表里的端口冲突永不豁免。其余核对（可迁入、属主、监听属主、重名）在面板代码里
        ! port_registered "$port" || die "端口 $port 已登记在项目表里，--takeover 不豁免项目端口"
        panel_api external takeover-begin "$takeover" --project "$name" --port "$port"
        TK_EXT=$takeover TK_PROJECT=$name
        src=external:$takeover
    else
        port_in_use "$port" && die "端口 $port 已被占用或已登记"
        if [ -f "$EXTERNAL" ]; then panel_api external precheck "$name" "$port"; fi
    fi

    trap create_rollback EXIT
    # 权威登记：EXTERNAL→PROJECTS 两把锁内复查名称/端口唯一并追加登记行（来源列第 5 列同行写入）。
    # 先登记再建账号：register 失败说明撞了并发登记，此时什么都没建，不需要回滚账号
    panel_api external register "$name" --port "$port" "--contact=$contact" ${src:+"--source=$src"}
    TK_REGISTERED=1
    useradd --create-home --home-dir "$SRV_ROOT/$name" --shell /bin/bash --user-group --groups "$GROUP" "$name"
    chmod 750 "$SRV_ROOT/$name"
    install -d -o "$name" -g "$name" -m 750 "$SRV_ROOT/$name"/{repo,config,data,backups}
    install -o root -g root -m 644 /dev/null "$KEYS_DIR/$name"
    loginctl enable-linger "$name"
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        usermod -aG "$name" "$SUDO_USER"
    fi
    TK_EXT="" TK_REGISTERED=""
    trap - EXIT
    echo "已创建项目账号 $name：目录 $SRV_ROOT/$name/{repo,config,data,backups}，端口 $port；尚无授权公钥，用 grant 添加"
    if [ -n "$takeover" ]; then
        echo "已从外部服务 $takeover 迁入：旧服务仍占着端口 $port，新服务必须等旧进程停掉后再启动（步骤见 references/project-accounts.md「迁入已有服务」）"
    fi
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
    local name port contact created source
    # 用 \037 做分隔：tab 是空白型 IFS，联系人为空时 read 会把空字段吞掉导致错位
    while IFS=$'\037' read -r name port contact created source; do
        [[ "$name" == \#* || -z "$name" ]] && continue
        echo "$name  端口 $port  联系人 ${contact:--}  创建 $created${source:+  来源 $source}"
        if ! id "$name" >/dev/null 2>&1; then echo "  （账号已不存在）"; continue; fi
        if is_disabled "$name"; then echo "  状态：停用"; else echo "  状态：启用"; fi
        keys_of "$name" | sed 's/^/  公钥 /'
    done < <(awk -F'\t' 'BEGIN { OFS = "\037" } !/^#/ && NF { print $1, $2, $3, $4, $5 }' "$REGISTRY")
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
    local name=${1:-} purge=0 uid n dest
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
    if ! id "$name" >/dev/null 2>&1; then
        # create 在 register 之后、useradd 之前被 SIGKILL/断电：登记行在、账号没有。只清残留，保目录，不 userdel。
        registry_has "$name" || die "$name 不是 $GROUP 项目账号"
        registry_remove "$name"
        release_external_target "$name"
        echo "账号 $name 不存在（登记后、建账号前被中断的残留）：已清除登记表条目与迁入标记；目录 $SRV_ROOT/$name 保留，未执行 userdel，--purge 不清理该目录"
        return 0
    fi
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
    registry_remove "$name"
    # 迁入来的项目：清掉外部服务上指向它的迁入目标。EXTERNAL.tsv 不存在、条目已不在时静默跳过（set -e 下不能失败）
    release_external_target "$name"

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

# 外部服务：参数原样透传给面板代码（读写、校验、属主判定都在那里，调用者身份取 SUDO_USER），这里只校验名称
cmd_external() {
    local sub=${1:-}
    need_installed
    case $sub in
        add)
            [ -n "${2:-}" ] || die "用法：external add <名称> --port N --owner <账号> [--unit …] [--desc …] [--migrate]"
            valid_name "$2"
            shift
            panel_api external add "$@" ;;
        remove)
            [ -n "${2:-}" ] || die "用法：external remove <名称> [--cancel-migration]"
            valid_name "$2"
            shift
            panel_api external remove "$@" ;;
        list)
            [ "${2:-}" = --json ] && [ $# -eq 2 ] || die "用法：external list --json"
            panel_api external list ;;
        *) die "用法：external <add|remove|list> …" ;;
    esac
}

cmd_takeover_copy() {
    [ -n "${1:-}" ] && [ -n "${2:-}" ] || die "用法：takeover-copy <项目> <源目录> [--to data|config]"
    valid_name "$1"
    need_installed
    panel_api takeover-copy "$@"
}

# 注释：shell 只做参数透传与基本用法检查（校验、存在性、加锁与原子写都在面板代码里）。
# 只认 --set=<文本> / --set=- / --clear 三种形态：= 形态消除以 - 开头文本的歧义，正文经 stdin 不进 argv / sudo 日志。
# 用法/互斥错误退出码 2（与面板 argparse 同口径），面板的校验失败为 1、项目不存在为 2。
cmd_note() {
    local project=${1:-} arg set_opt=0 clear_opt=0
    [ -n "$project" ] && [ $# -ge 2 ] || die_usage "用法：note <项目> --set=<文本> | note <项目> --set=- | note <项目> --clear"
    valid_name "$project"
    need_installed
    for arg in "${@:2}"; do
        case $arg in
            --set=*) set_opt=1 ;;
            --clear) clear_opt=1 ;;
            *) die_usage "用法：note <项目> --set=<文本> | note <项目> --set=- | note <项目> --clear" ;;
        esac
    done
    [ "$set_opt" -ne "$clear_opt" ] || die_usage "--set=<文本> 与 --clear 必须二选一"
    panel_api note "$@"
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
    external) shift; cmd_external "$@" ;;
    takeover-copy) shift; cmd_takeover_copy "$@" ;;
    note) shift; cmd_note "$@" ;;
    *) sed -n '2,/^set -euo pipefail$/{/^set -euo pipefail$/!p}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
