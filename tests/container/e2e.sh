#!/bin/bash
# 容器端到端：外部服务登记 + 迁入。以 root 运行；委托账号 admin 经 sudo -n 调用 srv-project（SUDO_USER=admin）。
PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); echo "  PASS  $*"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL  $*"; }
adm()  { runuser -u admin -- sudo -n /usr/local/sbin/srv-project "$@"; }
oth()  { SUDO_USER=otheru /usr/local/sbin/srv-project "$@"; }   # 模拟第二个委托账号
rt()   { /usr/local/sbin/srv-project "$@"; }
# expect_ok <说明> <命令...>   expect_fail <说明> <错误里应含的文字> <命令...>
expect_ok()   { local d=$1; shift; local out; if out=$("$@" 2>&1); then ok "$d"; else bad "$d -> $out"; fi; }
expect_fail() { local d=$1 pat=$2; shift 2; local out; if out=$("$@" 2>&1); then bad "$d (本应失败) -> $out"; else if grep -q -- "$pat" <<<"$out"; then ok "$d  [$(head -1 <<<"$out" | cut -c1-90)]"; else bad "$d (失败原因不符) -> $out"; fi; fi; }
j() { python3 -c "import json,sys; d=json.load(sys.stdin); $1"; }
section() { echo; echo "== $*"; }
listener() { local user=$1 port=$2 dir=$3; runuser -u "$user" -- setsid nohup python3 -m http.server "$port" --directory "$dir" >/dev/null 2>&1 & sleep 1; }
fetch() { python3 -c "import urllib.request,sys; print(urllib.request.urlopen('http://127.0.0.1:$1/$2', timeout=5).read().decode().strip())"; }

section "0. 准备：旧服务（admin 属主 9200，带 data 目录）、root 的服务 9300、otheru 的服务 9400"
install -d -o admin -g admin /home/admin/legacy-data /home/admin/legacy-data/sub
echo "legacy-hello" >/home/admin/legacy-data/hello.txt
head -c 1000 /dev/zero | tr '\0' 'a' >/home/admin/legacy-data/sub/blob.dat
chown -R admin:admin /home/admin/legacy-data
install -d -o otheru -g otheru /home/otheru/odata; echo secret >/home/otheru/odata/s.txt
mkdir -p /tmp/rootsvc; echo r >/tmp/rootsvc/r.txt
listener admin 9200 /home/admin/legacy-data
listener root 9300 /tmp/rootsvc
listener otheru 9400 /home/otheru/odata
ss -Hltnp | grep -E ':(9200|9300|9400) ' | awk '{print "   listening", $4}'

section "1. 没有 EXTERNAL.tsv 时 delete 项目静默跳过"
expect_ok "create plainp" adm create plainp --port 9500
expect_ok "disable plainp" rt disable plainp
expect_ok "delete plainp（EXTERNAL.tsv 不存在）" rt delete plainp --purge
[ ! -e /srv/EXTERNAL.tsv ] && ok "EXTERNAL.tsv 仍不存在" || bad "EXTERNAL.tsv 不应被创建"
expect_ok "重新 create plainp（后面做对照）" adm create plainp --port 9500

section "2. 兼容：4 列旧登记行"
printf 'legacy4\t3999\t\t2026-01-01 00:00:00\n' >>/srv/PROJECTS.tsv
expect_ok "list 人类可读不错位" bash -c "/usr/local/sbin/srv-project list | grep -q 'legacy4  端口 3999  联系人 -  创建 2026-01-01 00:00:00'"
rt list --json | j "p=[x for x in d['projects'] if x['name']=='legacy4'][0]; assert p['source'] is None and p['port']==3999; print('   json source =', p['source'])" && ok "list --json 4 列行 source=null" || bad "list --json 4 列"
sed -i '/^legacy4\t/d' /srv/PROJECTS.tsv

section "3. 委托账号越权负例"
expect_fail "--migrate 登记 root 的服务" "只能迁入调用者自己" adm external add rootsvc --port 9300 --owner root --migrate
expect_fail "--migrate 登记别人（otheru）的服务" "只能迁入调用者自己" adm external add othersvc --port 9400 --owner otheru --migrate
expect_fail "登记别人正在监听的端口（声称属主是自己）" "不符" adm external add stolen --port 9400 --owner admin
expect_fail "登记项目账号做属主" "项目账号" adm external add bad1 --port 9801 --owner plainp
expect_fail "登记项目的端口" "已登记为项目" adm external add bad2 --port 9500 --owner admin
expect_fail "与项目重名" "重名" adm external add plainp --port 9802 --owner admin
expect_fail "desc 含制表符" "控制字符" adm external add bad3 --port 9803 --owner admin --desc $'a\tb'
expect_fail "unit 格式错" "unit" adm external add bad4 --port 9804 --owner admin --unit "evil.service"
expect_fail "端口 < 1024 不能迁入" "低于 1024" adm external add lowp --port 900 --owner admin --migrate
expect_fail "名称非法" "项目名" adm external add 'Bad;name' --port 9805 --owner admin
[ ! -e /srv/EXTERNAL.tsv ] && ok "以上全部失败后 EXTERNAL.tsv 仍不存在" || { bad "EXTERNAL.tsv 被写入"; cat /srv/EXTERNAL.tsv; }

section "4. 正常登记"
expect_ok "admin 登记 oldsvc（可迁入）" adm external add oldsvc --port 9200 --owner admin --desc "旧静态服务" --unit "user:legacy.service" --migrate
expect_ok "root 只读登记 rootsvc" rt external add rootsvc --port 9300 --owner root --desc "root 的服务"
expect_ok "admin 只读登记 otheru 的服务" adm external add othersvc --port 9400 --owner otheru
expect_ok "登记未监听端口（带说明）" adm external add idle --port 9990 --owner admin
grep -q "登记时未监听" /srv/EXTERNAL.tsv && ok "说明里注明登记时未监听" || bad "缺少登记时未监听"
[ "$(stat -c %a:%U /srv/EXTERNAL.tsv)" = "644:root" ] && ok "EXTERNAL.tsv 644 root" || bad "权限 $(stat -c %a:%U /srv/EXTERNAL.tsv)"
adm external list --json | j "e={x['name']:x for x in d['externals']}; assert e['oldsvc']['migratable'] and not e['rootsvc']['migratable'] and e['oldsvc']['owner_match'] is True and e['idle']['listening'] is False; print('   externals:', sorted(e))" && ok "external list --json" || bad "external list --json"
expect_fail "重复登记" "已登记" adm external add oldsvc --port 9201 --owner admin
expect_fail "create 撞外部服务端口（不带 takeover）" "占用\|登记" adm create clash1 --port 9990
expect_fail "create 撞外部服务名称" "重名" adm create oldsvc --port 9991

section "5. takeover 越权负例"
expect_fail "takeover 非可迁入的外部服务" "未标记可迁入" adm create t1 --takeover rootsvc --port 9300
expect_fail "takeover 别人（oth）的可迁入条目" "属主" oth create t2 --takeover oldsvc --port 9200
expect_fail "takeover 抢已登记项目的端口（端口不等于外部服务端口）" "项目表\|必须等于" adm create t3 --takeover oldsvc --port 9500
# root 手工把 oldsvc 的端口塞进项目表，模拟项目端口冲突：takeover 永不豁免
printf 'sneaky\t9200\t\t2026-10-31 00:00:00\n' >>/srv/PROJECTS.tsv
expect_fail "takeover 碰到 PROJECTS.tsv 已有的端口" "不豁免" adm create t4 --takeover oldsvc --port 9200
sed -i '/^sneaky\t/d' /srv/PROJECTS.tsv
grep -P '^oldsvc\t' /srv/EXTERNAL.tsv | awk -F'\t' '$6 == "" {print "   target 仍为空"}' | grep -q . && ok "失败后迁入标记未留下" || bad "迁入标记残留"
expect_fail "takeover 与外部服务重名" "重名" adm create oldsvc --takeover oldsvc --port 9200

section "5b. 端口归一化：前导 0 不能绕过「项目端口永不豁免」"
expect_fail "create 拒绝带前导 0 的端口" "前导 0" adm create zeropad --port 09500
printf 'sneaky0\t09200\t\t2026-10-31 00:00:00\n' >>/srv/PROJECTS.tsv
expect_fail "项目表里写成 09200 的行仍拦住 takeover" "不豁免" adm create t5 --takeover oldsvc --port 9200
expect_fail "external add 同样把 09200 当作项目端口" "已登记为项目" adm external add zz1 --port 09200 --owner admin
sed -i '/^sneaky0\t/d' /srv/PROJECTS.tsv

section "5c. 登记表并发：5 个 create 同时追加不丢行"
pids=()
for i in 1 2 3 4 5; do adm create par$i --port $((9600 + i)) >/dev/null 2>&1 & pids+=($!); done
wait "${pids[@]}"      # 只等这几个，别等前面后台起的旧服务
n=$(grep -c '^par[1-5]\b' /srv/PROJECTS.tsv); [ "$n" = 5 ] && ok "5 行都在" || bad "只有 $n 行"
for i in 1 2 3 4 5; do rt disable par$i >/dev/null 2>&1; done
pids=()
for i in 1 2 3 4 5; do rt delete par$i --purge >/dev/null 2>&1 & pids+=($!); done
wait "${pids[@]}"
! grep -q '^par[1-5]\b' /srv/PROJECTS.tsv && ok "并发删除后全部清除" || bad "并发删除残留"

section "6. 失败注入：create 中途失败回滚迁入标记"
groupadd failgrp
expect_fail "useradd 因同名组失败" "" adm create failgrp --takeover oldsvc --port 9200
if awk -F'\t' '$1 == "oldsvc" && $6 == "" {f=1} END {exit !f}' /srv/EXTERNAL.tsv; then ok "迁入标记已回滚（target 为空）"; else bad "迁入标记未回滚"; grep oldsvc /srv/EXTERNAL.tsv; fi
adm create failgrp --takeover oldsvc --port 9200 2>&1 | grep -q "已回滚" && ok "输出提示已回滚" || bad "未提示回滚"
groupdel failgrp
! grep -q failgrp /srv/PROJECTS.tsv && ok "PROJECTS.tsv 没有残留行" || bad "PROJECTS.tsv 残留"

section "7. 迁入：create --takeover"
expect_ok "admin 迁入 oldsvc → newapp" adm create newapp --takeover oldsvc --port 9200 --contact "测试"
grep -P '^newapp\t9200\t测试\t.*\texternal:oldsvc$' /srv/PROJECTS.tsv >/dev/null && ok "PROJECTS.tsv 第 5 列 = external:oldsvc" || { bad "来源列"; cat -A /srv/PROJECTS.tsv; }
awk -F'\t' '$1 == "oldsvc" && $6 == "newapp" {f=1} END {exit !f}' /srv/EXTERNAL.tsv && ok "EXTERNAL.tsv 迁入目标 = newapp" || bad "迁入目标"
rt status --json | j "
e=[x for x in d['externals'] if x['name']=='oldsvc'][0]
p=[x for x in d['projects'] if x['name']=='newapp'][0]
l=[x for x in d['global']['listeners'] if x['port']==9200][0]
assert e['migrate_state']=='迁移中' and e['problems']==[], e
assert p['source']=='external:oldsvc' and p['problems']==[] and any('迁移中' in n for n in p['notes']), p
assert l['project']=='newapp' and l['external']=='oldsvc' and l['migrate_state']=='迁移中', l
print('   外部服务状态:', e['migrate_state'], '| 项目 notes:', p['notes'], '| 监听标注:', l)
" && ok "status：迁移中，新项目只有 notes 没有 problems，listeners 同时标 project/external/migrate_state" || bad "status 迁移中"
rt status --json newapp | j "assert 'externals' not in d and 'global' not in d; print('   单项目 status 不含 externals/global')" && ok "单项目 status 不带 externals" || bad "单项目 status"
expect_fail "迁入中 external remove 被拒" "cancel-migration" adm external remove oldsvc
expect_fail "迁入中再次 takeover 被拒" "项目表\|已在迁入" adm create again1 --takeover oldsvc --port 9200
expect_fail "他人（第二个委托账号）不能取消别人发起的迁移" "只有 root 或它的属主" oth external remove oldsvc --cancel-migration
expect_fail "他人不能删迁入中的条目" "只有 root 或它的属主" oth external remove oldsvc
grep -q 'external:oldsvc' /srv/PROJECTS.tsv && awk -F'\t' '$1 == "oldsvc" && $6 == "newapp" {f=1} END {exit !f}' /srv/EXTERNAL.tsv && ok "越权取消后迁入标记与项目来源都还在" || bad "越权操作改动了登记"
expect_fail "他人不能删别人的只读登记（rootsvc 由 root 登记）" "登记人" oth external remove rootsvc
expect_ok "登记人（admin）可以删自己登记的只读条目 idle" adm external remove idle

section "8. 申请方部署（密钥授权 + ssh 写代码 + 用户服务 enable 不启动）"
ssh-keygen -q -t ed25519 -N '' -f /tmp/devkey
expect_ok "grant 公钥" rt grant newapp /tmp/devkey.pub --label dev
rt invite newapp --label dev2 >/tmp/invite.txt 2>/tmp/invite.err && ok "invite 可生成 [$(head -c 12 /tmp/invite.txt)...]" || echo "   (invite: $(head -c 120 /tmp/invite.err)) —— 面板配置未装，设备邀请流程本次改动未涉及"
/usr/sbin/sshd -o PasswordAuthentication=no 2>/dev/null || true; sleep 1
ssh -i /tmp/devkey -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR newapp@127.0.0.1 'mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/web.service <<EOF
[Service]
ExecStart=/usr/bin/python3 -m http.server 9200 --directory /srv/newapp/data/legacy-data
[Install]
WantedBy=default.target
EOF
echo deployed-by-newapp > ~/repo/VERSION' && ok "申请方 ssh 登录并部署代码与 unit 文件" || bad "ssh 部署"
loginctl show-user newapp -p Linger 2>/dev/null | head -1
systemctl --user --machine=newapp@ daemon-reload 2>&1 | head -2
systemctl --user --machine=newapp@ enable web.service 2>&1 | tail -1

section "9. takeover-copy（旧属主在服务器终端执行；旧服务不停）"
expect_fail "项目不是迁入来的（plainp）" "不是从外部服务迁入" adm takeover-copy plainp /home/admin/legacy-data
expect_fail "他人（otheru）的目录（属主不对）" "属主" adm takeover-copy newapp /home/otheru/odata
expect_fail "/srv 下的目录" "不能位于" adm takeover-copy newapp /srv/newapp/repo
ln -s /home/admin/legacy-data /home/admin/link-data
expect_fail "路径含符号链接" "符号链接" adm takeover-copy newapp /home/admin/link-data
expect_fail "相对路径" "绝对路径" adm takeover-copy newapp legacy-data
expect_fail "另一个委托账号（非旧服务属主）" "属主" oth takeover-copy newapp /home/admin/legacy-data
expect_fail "--to 非法" "invalid choice" adm takeover-copy newapp /home/admin/legacy-data --to repo
echo "   旧服务仍在响应: $(fetch 9200 hello.txt)"
adm takeover-copy newapp /home/admin/legacy-data && ok "takeover-copy 成功" || bad "takeover-copy"
expect_fail "目标已存在则拒绝" "已存在" adm takeover-copy newapp /home/admin/legacy-data
[ "$(cat /srv/newapp/data/legacy-data/hello.txt)" = legacy-hello ] && ok "文件内容已复制" || bad "文件内容"
[ "$(stat -c %U /srv/newapp/data/legacy-data/sub/blob.dat)" = newapp ] && ok "属主已改为项目账号" || bad "属主 $(stat -c %U /srv/newapp/data/legacy-data/sub/blob.dat)"
[ -z "$(ls -A /srv/.takeover)" ] && ok "暂存目录已清空" || bad "暂存目录残留"
[ "$(stat -c %U /home/admin/legacy-data/hello.txt)" = admin ] && ok "源目录未被改动" || bad "源目录被改动"
tail -3 /var/log/srv-panel.log 2>/dev/null | cut -c1-160

section "10. 停旧进程，启动新服务，确认已切换"
pkill -u admin -f "http.server 9200"; sleep 1
ss -Hltn "sport = :9200" | grep -q . && bad "旧进程未停" || ok "旧进程已停，9200 空闲"
rt status --json | j "e=[x for x in d['externals'] if x['name']=='oldsvc'][0]; assert e['migrate_state']=='迁移中' and e['listening'] is False; print('   间隙状态:', e['migrate_state'], e['notes'])" && ok "间隙：仍为迁移中、未监听" || bad "间隙状态"
systemctl --user --machine=newapp@ start web.service 2>&1 | tail -1; sleep 2
echo "   新服务响应: $(fetch 9200 hello.txt)"
[ "$(fetch 9200 hello.txt)" = legacy-hello ] && ok "新服务（newapp 属主）在 9200 提供迁入数据" || bad "新服务不响应"
rt status --json | j "
e=[x for x in d['externals'] if x['name']=='oldsvc'][0]
p=[x for x in d['projects'] if x['name']=='newapp'][0]
assert e['migrate_state']=='已切换' and e['problems']==[], e
assert p['problems']==[] and p['listening'] and p['listen_owner']=='newapp', p
print('   状态:', e['migrate_state'], e['notes'])
" && ok "status：已切换，无 problems" || bad "已切换状态"

section "11. 确认已切换后 external remove（项目来源保留）"
expect_ok "admin external remove oldsvc" adm external remove oldsvc
! grep -q '^oldsvc' /srv/EXTERNAL.tsv && ok "登记已删" || bad "登记仍在"
grep -q 'external:oldsvc' /srv/PROJECTS.tsv && ok "项目来源保留（抽屉仍显示由 oldsvc 迁入）" || bad "项目来源丢失"
rt status --json | j "names=[x['name'] for x in d['externals']]; assert 'oldsvc' not in names; print('   剩余外部服务:', names)" && ok "externals 列表不含 oldsvc" || bad "externals 仍含"
expect_fail "已切换后 takeover-copy 不再允许" "不在迁入中" adm takeover-copy newapp /home/admin/legacy-data --to config

section "12. 取消迁移：--cancel-migration 清项目来源；delete 清迁入目标"
pkill -f "http.server 9990" ; true
listener admin 9701 /home/admin/legacy-data
expect_ok "登记 ext2（可迁入）" adm external add ext2 --port 9701 --owner admin --migrate
expect_ok "迁入 app2" adm create app2 --takeover ext2 --port 9701
expect_fail "账号存在但未停用仍拒绝 delete" "尚未停用" rt delete app2
expect_ok "cancel-migration 删除" adm external remove ext2 --cancel-migration
awk -F'\t' '$1=="app2"{print NF==5 ? "src=" $5 : "cols=" NF}' /srv/PROJECTS.tsv | grep -q '^src=$\|cols=4' && ok "app2 来源已清空" || { bad "app2 来源"; grep app2 /srv/PROJECTS.tsv | cat -A; }
expect_ok "登记 ext3" adm external add ext3 --port 9702 --owner admin --migrate
expect_ok "迁入 app3" adm create app3 --takeover ext3 --port 9702
expect_ok "disable app3" rt disable app3
expect_ok "delete app3" rt delete app3 --purge
awk -F'\t' '$1=="ext3" && $6==""{f=1} END{exit !f}' /srv/EXTERNAL.tsv && ok "delete 后 ext3 迁入目标已清空，可重新迁入" || { bad "ext3 目标"; grep ext3 /srv/EXTERNAL.tsv; }
expect_ok "delete 时条目已不在也不报错：先删 ext3 再删 app2" bash -c "/usr/local/sbin/srv-project disable app2 && /usr/local/sbin/srv-project delete app2 --purge"

section "12b. 中断残留：登记行存在、账号不存在"
expect_ok "登记 orphsvc（只读）" adm external add orphsvc --port 9502 --owner admin
printf 'orphanp\t9502\t\t2026-01-01 00:00:00\texternal:orphsvc\n' >>/srv/PROJECTS.tsv
awk -F'\t' -v OFS='\t' '$1=="orphsvc"{$6="orphanp"} {print}' /srv/EXTERNAL.tsv >/tmp/ext.tmp && cat /tmp/ext.tmp >/srv/EXTERNAL.tsv
install -d -o root -g root /srv/orphanp/data
echo keep >/srv/orphanp/data/keep.txt
id orphanp >/dev/null 2>&1 && bad "测试前提：orphanp 账号不应存在" || ok "测试前提：orphanp 账号不存在"
expect_ok "delete 只清理中断残留的登记行" rt delete orphanp
grep -q '^orphanp\t' /srv/PROJECTS.tsv && bad "残留行未清" || ok "残留行已清"
awk -F'\t' '$1=="orphsvc" && $6==""{f=1} END{exit !f}' /srv/EXTERNAL.tsv && ok "迁入标记已清" || { bad "迁入标记未清"; grep orphsvc /srv/EXTERNAL.tsv; }
[ -f /srv/orphanp/data/keep.txt ] && ok "磁盘目录保留" || bad "磁盘目录被删"
printf 'otheru\t9503\t\t2026-01-01 00:00:00\t\n' >>/srv/PROJECTS.tsv
expect_fail "账号存在但不在 srvproj 仍拒绝" "不是 srvproj 项目账号" rt delete otheru
sed -i '/^otheru\t/d' /srv/PROJECTS.tsv
expect_fail "账号不存在且无登记行仍拒绝" "不是 srvproj 项目账号" rt delete ghostp
expect_fail "外部服务名不能借 delete 清掉" "不是 srvproj 项目账号" rt delete orphsvc

section "13. 迁入模式的 tsv 兼容：list 与 usage"
rt list | grep -E "newapp" | cut -c1-120
rt 2>&1 | grep -q "set -euo" && bad "usage 泄漏了脚本代码" || ok "usage 只含头部注释"

echo
echo "RESULT: pass=$PASS fail=$FAIL"
