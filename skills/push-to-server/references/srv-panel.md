# /srv 项目管理面板

给服务器管理员用的网页面板，管理 `srv-project`（`scripts/srv_project.sh`）创建的 `/srv/<项目>` 项目账号。局域网内浏览器直接用 HTTPS 打开，账号密码登录。界面为中文服务端渲染网页，单文件、只用 Python 3.12 标准库，无外部 JS/CSS。面板进程以 root 运行。

文件：

- `panel/srv_panel.py`：面板本体
- `panel/install_panel.sh`：安装、升级、忘记密码重置（root 运行，可重复执行）

## 安装

前提：服务器已安装 `/usr/local/sbin/srv-project` 并执行过 `srv-project install`，且有 `python3` 和 `openssl`。

```bash
sudo bash panel/install_panel.sh [--lan-ip <服务器IP>] [--allow-from <网段CIDR>]... [--enroll-only | --full]
```

- `--lan-ip`：局域网 IP，写入证书并作为监听地址。不传则自动取默认路由出口网卡的 IPv4，并在终端打印，不对请加参数重跑。服务器应使用固定 IP，IP 变了要重新运行脚本（会重签证书）
- `--enroll-only` / `--full`：登记模式开关，见下文「登记模式」。两者都不给则保持原有模式（首次安装为完整模式）
- `--allow-from`：只允许这些来源网段访问，可重复，例如 `--allow-from 192.168.1.0/24`。默认不限制。服务器本机（回环来源）始终放行，避免把自己锁在外面

脚本会：

1. 把 `srv_panel.py` 装到 `/usr/local/lib/srv-panel/`
2. 用 `openssl` 生成自签证书 `/etc/srv-panel/tls.crt` 和 `tls.key`（key 权限 600，有效期 3 年，SAN 含局域网 IP、`127.0.0.1`、`localhost`）。证书已存在、SAN 含当前 IP 且 30 天内不到期时沿用
3. 完整模式下，没有用户库时创建超级管理员 `admin`，随机初始密码写入 `/etc/srv-panel/initial-password`（root 600）。终端只提示文件位置，不打印密码
4. 发现旧版令牌文件 `/etc/srv-panel/token` 就删除并提示（令牌登录已取消）
5. 写 `/etc/systemd/system/srv-panel.service`（root 运行，`Restart=on-failure`），`enable` 并启动

重复执行即升级面板代码并重启服务，用户库、证书、初始密码文件（尚未被消费时）都保持不变。

## 首次登录与访问

1. 浏览器打开 `https://<服务器IP>:9300`（也可在服务器本机打开 `https://127.0.0.1:9300`）
2. 第一次会警告证书不受信任（自签证书）。点「高级 → 继续访问」确认一次即可，此后该浏览器记住这个证书
3. 用户名 `admin`，密码用 `sudo cat /etc/srv-panel/initial-password` 查看
4. 登录后会被强制进入「修改密码」页，改密前其它页面一律重定向回来。新密码至少 8 位，须同时包含大写字母、小写字母、数字和特殊符号，不能与当前（初始）密码或用户名相同
5. 改密成功后 `initial-password` 文件被删除，`admin` 即正式的超级管理员

没有人能「谁先访问谁当管理员」：首个账号只能凭服务器上 root 才能读的初始密码登录。

## 功能

**总览**（`/`）：每个项目一张卡片

- 启用 / 停用状态（`/etc/shadow` 第 8 字段为 1 即停用）
- 登记端口是否在监听、监听进程属主是否为该项目账号
- 问题提示：登记端口被其他账号占用；两个项目登记同一端口；账号缺失；账号不在 `srvproj` 组
- 已授权公钥（标签、指纹、来源限制）
- 用户服务及状态（`systemctl --user --machine=<账号>@ list-units`；只显示该项目自己的服务，隐藏 `dbus.service` 和无单元文件的 inactive 系统单元）
- `repo/` 的 git HEAD、`/srv/<项目>` 磁盘占用

**项目详情**（`/project/<项目>`）：在总览基础上提供全部操作，并做环境依赖检测

- 环境依赖：以该账号身份运行 `runuser -l <账号> -c "bash -lic …"`（登录加交互 shell，`~/.profile`、`~/.bashrc` 里加的 PATH，例如 `~/.local/node/bin`，也会生效），报告 git、node、npm、python3、docker 的可用性、版本和路径
- 声明比对：`package.json` 的 `engines.node`、`.nvmrc`、`requirements.txt`、`pyproject.toml`（含 `requires-python`）与实际版本是否匹配。支持 `>= > <= < = ^ ~ x` 和 `||`，看不懂的约束标为「无法自动判断」，不误报
- 停用的项目账号被锁定，不做环境检测

**全局检查**（`/global`）：`srv-project`、sshd 规则、公钥目录、登记表是否在；`/srv` 下对 other 可读的目录（警告）；`/srv` 下未登记的目录；本机所有监听端口及属主，标出登记的项目端口与未登记端口。

**项目操作**：新建项目、授权公钥（粘贴单行公钥，经 stdin 走 `grant <项目> -`）、吊销、停用、启用、用户服务 restart / start / stop、查看用户服务最近 100 行日志。停用、吊销、停止服务先进入确认页，点「确认执行」才真正执行（页面内表单，不用 JS `confirm()`）。`srv-project` / `systemctl` 的命令、退出码、标准输出、标准错误原样回显到结果页。

## 用户管理

角色有两种：

- **超级管理员**（`super`）：能做全部项目操作，并管理用户。首个账号 `admin` 是超级管理员
- **管理员**（`admin`）：能做全部项目操作，不能管理用户

超级管理员在「用户管理」页（`/users`）可以：

- **新建管理员**：系统生成一次性初始密码，只在结果页显示一次，请当面或经安全渠道交给对方。对方首次登录必须改密
- **重置密码**（需确认）：旧密码立即失效，生成新的一次性初始密码，强制改密，并注销该用户的全部登录会话
- **删除用户**（需确认）：立即注销其全部会话。不能删自己，也不能删最后一个超级管理员

每个用户都能在「修改密码」页改自己的密码（改密后其余会话被注销）。超级管理员要改自己密码也走这里，不能对自己做「重置」。

## 忘记密码

在服务器上以 root 执行：

```bash
sudo bash panel/install_panel.sh --reset-admin
```

- `admin` 存在：生成新的初始密码写入 `/etc/srv-panel/initial-password`，把 `admin` 设回「必须改密」，其他用户不动
- `admin` 不存在（比如被改名或损坏）：清空用户库，重新创建 `admin`，回到首次登录流程（其他用户需要重新创建）
- 然后重启面板服务，所有已登录会话立即失效

之后按「首次登录」的步骤登录并改密。

## 安全模型

- 必须 root 启动，否则退出
- **只走 HTTPS**：非回环地址的监听必须同时给 `--allow-remote` 和 TLS 证书与私钥，缺任一项拒绝启动，不提供明文 HTTP。TLS 最低 1.2；启用 TLS 时会话 cookie 带 `Secure`
- **监听地址**：服务只绑定两个地址，局域网 IP 和 `127.0.0.1`（同一进程两个监听）。不绑 `0.0.0.0`，所以服务器的其他网卡（docker 网桥、VPN 等）上不开放端口，Host 头校验只是第二层
- **来源限制**：可选 `--allow-from` 白名单；不在名单内的来源 IP 直接返回 403 并写审计日志（同一来源一分钟最多记一条）。回环来源始终放行
- **密码**：`hashlib.scrypt`（N=16384, r=8, p=1），每个用户单独 16 字节随机盐，常量时间比较。用户库 `/etc/srv-panel/users.json`（root 600）原子写入（先写同目录临时文件，`fsync` 后 `rename`）。用户不存在时也做一次同等代价的校验，响应不区分「用户不存在」和「密码错」
- **登录限流按来源 IP 计数**：同一 IP 连续失败 5 次锁定 5 分钟，锁定期内即使密码正确也拒绝；成功登录清零；其他 IP 不受影响。限流状态在内存里，重启清空
- **会话**：登录后换 `HttpOnly; SameSite=Strict` cookie（8 小时，随机 256 位，保存在进程内存），绑定用户名。删除用户、重置密码、用户改密都会注销相应会话；服务重启全部失效
- 所有 POST 带与会话绑定的 CSRF 令牌；Host 头只接受 `127.0.0.1` / `localhost` / 安装时指定的局域网 IP（可带本服务端口），防 DNS rebinding
- 输出全部 `html.escape`；响应带 CSP（无脚本）、`X-Frame-Options: DENY`、`no-store`；请求体限 64KB，连接带 30 秒超时，TLS 握手在工作线程里并带超时
- 项目名用与 `srv_project.sh` 相同的正则校验并要求已登记；服务名、标签、来源地址、公钥引用均有白名单；变更操作只通过参数列表调用 `srv-project` / `systemctl`，不经 shell
- 项目目录由项目账号控制，所以：读 `repo/` 的版本声明文件只读真实路径仍在 `/srv/<项目>/` 内的小普通文件；读 git HEAD 用 `runuser -u <账号>` 降权运行，避免 root 执行仓库里可触发命令的 git 配置
- 审计日志 `/var/log/srv-panel.log`（600）：时间、动作、参数、来源 IP、退出码。覆盖登录成败、锁定、改密、用户新建/重置/删除、IP 拒绝、CSRF 拒绝和全部项目操作。授权公钥只记标签、来源限制和指纹。**任何密码都不记**

## 登记模式（enroll-only）

服务器上有知识库后台的【项目运维】时，项目管理走知识库后台，独立面板只负责设备登记：

```bash
sudo bash panel/install_panel.sh --enroll-only     # 打开登记模式
sudo bash panel/install_panel.sh --full            # 关闭登记模式，恢复管理页
```

- 登记模式写在 `/etc/srv-panel/panel.conf`（键 `enroll_only`，值 `true`），由面板服务读取并强制执行：**只开放 `POST /enroll`，其余任何路径、任何方法一律返回 404**（`GET /` 也是 404）
- 登记模式不依赖账号体系：没有 `users.json` 也能启动，安装时也不创建 `admin` 和初始密码；之前的用户库不会被删除，`--full` 后继续使用
- 模式只由 `--enroll-only` / `--full` 改变。不带这两个参数的重复安装（升级代码、证书临期重签、换 IP）**保留原有模式**，不会悄悄重新打开管理页。安装输出的最后会写明当前是哪种模式
- 设备邀请的生成、列表、作废仍由 `srv-project invite / invites / invite-revoke`（或知识库后台）完成，这些不经过网页，不受登记模式影响

## JSON 接口（`srv-project` 子命令）

知识库后台等程序通过 `sudo -n srv-project …` 调用下面的子命令；它们都转调 `srv_panel.py api …`，面板代码没装时报「请先安装管理面板」。`create`（含 `--takeover`）也依赖面板代码：它经 `api external register` 在 `EXTERNAL → PROJECTS` 锁内写登记行，所以只装过 `srv-project install`、没装面板的旧部署升级脚本后，必须先补 `install_panel.sh` 才能继续新建项目。**字段只能增加，不能删减或改名。** 所有命令失败时退出码非 0，原因写到 stderr；指定的项目不存在时退出码为 2。

```text
list --json
  {"projects":[{"name","port":int,"contact","created","account_exists":bool,"disabled":bool,
                "keys":[{"label","fingerprint","from":str|null}]}]}

status --json [<项目>]
  {"projects":[{…list 的全部字段…,
                "listening":bool, "listen_owner":str|null,
                "services":[{"unit","active","sub"}], "head":str|null, "disk_kb":int|null,
                "problems":[str]}],
   "global":{"sshd_rule":bool, "world_readable":[str],
             "listeners":[{"port":int,"owner":str|null,"project":str|null}]}}
  指定项目时不输出 "global"；项目不存在时退出码为 2

env --json <项目>
  {"project","disabled":bool,"tools":[{"name","available":bool,"version":str|null,"path":str|null}],
   "declarations":[{"file","requirement":str|null}],"findings":[str]}

info --json <项目>
  {"project","host","ssh_port":int,"account","dir","port":int,"disabled":bool,"keys":[…同上…],"text":"<原 info 的完整文本>"}

invites --json <项目>
  {"invites":[{"id","project","label","from":str|null,"created","expires","status":"pending|used|revoked|expired",
               "used":{"at","ip","device","fingerprint"}|null}]}

logs <项目> <服务> [--lines N]
  纯文本，N 默认 100、最多 1000

svc <项目> <start|stop|restart> <服务>
  人类可读文本 + 退出码

note <项目> --set=<文本> | --set=- | --clear
  设置或清除项目注释：单行 UTF-8，≤200 个 Unicode 码点，非空非纯空白，不含控制字符或孤立代理；
  保存时不改写原文（保留首尾空格）。--set=- 从 stdin 读正文（推荐：正文不经 argv / sudo 日志）；
  --set= 空值与纯空白拒绝，清除只能用 --clear；= 形态保证以 - 开头的文本不被误解为选项。
  成功 stdout 只输出 {"name","note":str|null}；同值 set 与已空 clear 幂等成功且不重写登记表；
  项目不存在或用法/互斥错误退出码 2；校验/写失败退出码 1，错误不回显注释正文

invite <项目> --label L [--hours N] [--from X]
  stdout 只有一行邀请文本，说明写到 stderr

invite-revoke <项目> <编号>

external list --json
  {"externals":[{"name","port":int,"owner","unit":str|null,"migratable":bool,"migrate_target":str|null,
                 "desc","created","listening":bool,"listen_owner":str|null,"owner_match":bool|null,
                 "migrate_state":"迁移中"|"已切换"|null,"problems":[str],"notes":[str]}]}

external add <名称> --port N --owner <账号> [--unit …] [--desc …] [--migrate]
  {"external":{…同上…},"notes":[str]}；校验失败退出码 1

external remove <名称> [--cancel-migration]
  人类可读文本；名称不存在退出码 2

create <项目> --port N [--contact C] [--takeover <外部服务>]
  人类可读文本

takeover-copy <项目> <源目录> [--to data|config]
  人类可读文本（含文件数和字节数）
```

实现说明：

- `list --json` 很轻：只读登记表、`/etc/shadow` 和公钥文件，指纹在进程内计算，不跑 `du`、`git`、`systemctl`，也不起任何子进程。
- `status --json` 复用面板总览的采集（`collect_all` / `collect_project`）。账号缺失的项目，`listening` 为 `false`，`listen_owner`、`head`、`disk_kb` 为 `null`，`keys`、`services` 为 `[]`，`disabled` 为 `false`，`problems` 里有原因，保证每条记录字段一致。`head` 只在能读到提交时才有值（否则为 `null`）。
- 新增字段（不影响约定）：`status` 的项目里有 `notes`、`services_error`；`status` 的 `global` 里有 `srv_project`、`unregistered_dirs`；`env` 里有 `findings_detail`（`level` + `text`）。
- 外部服务（`/srv/EXTERNAL.tsv`，登记规则与安全不变量见 [`project-accounts.md`](project-accounts.md)「迁入已有服务」）相关的新增字段：
  - `list` 和 `status` 的项目记录里有 `source`（迁入来的项目为 `external:<名称>`，否则 `null`）；知识库后台只让超级管理员看到它：运维管理员的项目记录在 `services/srv_ops.py` 里裁掉 `source` 与迁移提示；
  - `status` 无项目参数的全量输出里有顶层 `externals`（字段同 `external list --json`，不做服务、git、磁盘探测）；**指定项目的 `status` 不带 `externals`**；
  - `status` 的 `global.listeners` 每项多 `external`（该端口登记的外部服务名或 `null`）和 `migrate_state`；端口既属于项目又属于外部服务时 `project`、`external` 都写；
  - 迁入中的新项目，端口的监听属主正好是外部服务的登记属主时不算问题，只在项目的 `notes` 里写「迁移中：旧服务仍在监听」；
  - `migrate_state`：迁入目标已设置时，监听属主恰好是新项目账号为「已切换」，其余为「迁移中」；没有迁入目标为 `null`。
- 项目注释（`note` 子命令读写 `PROJECTS.tsv` 第 6 列，使用边界见 [`project-accounts.md`](project-accounts.md)「项目注释」）相关的新增字段与行为：
  - `list` 和 `status`（全量与单项目）的项目记录里有 `note`（没有注释为 `null`）。注释只进 JSON 输出：独立面板网页和人类可读 `srv-project list` 不展示；知识库后台仅超级管理员可见，运维管理员的记录同样裁掉 `note`；
  - `note` 的写入只持 `PROJECTS` 一把锁，在同一次完整读取里定位目标行并按列更新：项目不存在退出码 2 且**绝不追加新行**；同一项目出现重复登记行时 fail-closed 拒写；表里发现 CR/CRLF 时拒绝并提示格式异常；写回仍是临时文件 + `os.replace`；
  - `external remove --cancel-migration` 清来源只把第 5 列按列清空（不再用前 4 列重建行），注释（第 6 列）保留；失败提示也只要求按列清空、不要求物理删列。
- 独立面板的网页「全局检查」不显示外部服务，外部服务只在知识库后台【项目运维】里展示和维护。
- `PROJECTS.tsv` 第 5 列「来源」、第 6 列「注释」都可缺省；`install` 新建的文件表头是 6 列，已有 4/5 列的旧表原样兼容、不迁移。普通 `create`（含 `--takeover`）仍写 5 列，首次设置注释才把该行扩为 6 列，清除注释删掉第 6 字段（不留尾随 tab）。
- `invites` 和 `invite-revoke` 必须带项目参数。`invite-revoke <项目> <编号>` 会断言该邀请属于这个项目，不属于就报「找不到邀请」，退出码非 0；旧的单参数写法直接报用法错误。面板网页的作废走内部函数，不受影响。
- 校验：项目名用 `^[a-z][a-z0-9_-]{1,31}$`，服务名要以 `.service` 结尾（首字符字母或数字，允许 `@ . _ : -`），`--lines` 取 1–1000。

## 参数

```
srv_panel.py [--bind IP]... [--port 9300] [--allow-remote]
             [--tls-cert 证书 --tls-key 私钥] [--allowed-host 主机名或IP]...
             [--allow-from CIDR]... [--users-file /etc/srv-panel/users.json]
             [--initial-password-file /etc/srv-panel/initial-password]
             [--log-file /var/log/srv-panel.log]
srv_panel.py --bootstrap | --reset-admin        # 安装脚本调用
srv_panel.py --write-panel-conf --lan-ip IP --port N --tls-cert 证书 [--enroll-only | --full]   # 安装脚本调用
srv_panel.py invite create|list|revoke …        # srv-project invite* 转调
srv_panel.py api list|status|env|info|invites|logs|svc|external|takeover-copy|note …   # srv-project 的 JSON 与外部服务子命令转调
```

## 已知限制

- 自签证书不被浏览器预先信任，每个浏览器第一次要手动确认。证书到期（3 年）或局域网 IP 变化后需重新运行安装脚本
- 登录限流和会话都在内存里，面板重启后需要重新登录，限流计数清零
- 限流按来源 IP；经 NAT/代理访问时，同一出口的所有人共用一个计数
- 总览每次加载会为每个项目执行 `systemctl`、`ssh-keygen`、`runuser git`、`du`，项目很多或目录很大时较慢
- 用户服务日志依赖 journal 中带 `_UID` 的记录；服务自身 stdout 是否进入 journal 取决于单元配置
- 面板不删除项目（`srv-project` 没有 delete 子命令）
- 只有 `super` 和 `admin` 两种角色，不能把普通管理员提升为超级管理员

## 卸载

```bash
sudo systemctl disable --now srv-panel.service
sudo rm -f /etc/systemd/system/srv-panel.service
sudo systemctl daemon-reload
sudo rm -rf /usr/local/lib/srv-panel
sudo rm -rf /etc/srv-panel          # 删除用户库、证书和初始密码文件
sudo rm -f /var/log/srv-panel.log   # 可选：删除审计日志
```

卸载面板不影响任何项目账号、公钥和用户服务。
