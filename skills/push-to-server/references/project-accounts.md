# 共享 /srv 服务器的项目接入

一台服务器给多个人部署项目时，每个 `/srv/<项目>` 配一个独立的 Linux 账号，靠邀请完成接入。整个过程由双方的 agent 执行，人只做下面这几件事：

| 角色 | 人做的事 | agent 做的事 |
|---|---|---|
| 服务器管理员 | 决定给谁开哪个项目（说「批准」或在面板上点「邀请」）；把邀请转发给申请方 | 开项目账号、生成邀请 |
| 申请方（局域网用户） | 把邀请交给自己的 agent；首次登录应用后改密码 | 凭邀请登记设备、首次部署、配服务、交付访问地址和初始密码 |

每个 agent 只执行自己角色的阶段，不跨角色代办：申请方的 agent 不碰服务器 root 权限，管理员的 agent 不替申请方部署应用。

## 账号的权限边界

| 能做 | 不能做 |
|---|---|
| 读写 `/srv/<项目>`（家目录，750） | 读写其它项目、管理员家目录、`/srv` 根目录 |
| 用 `systemctl --user` 管自己的服务，登出后照常运行（linger） | `sudo`、加入 `docker` 组 |
| 在自己目录里装运行时（如 `~/.local/node`） | 密码登录、端口转发、agent 转发、X11 |
| 监听登记的端口（≥1024） | 改授权公钥（在 root 所有的 `/etc/ssh/srv-keys/<账号>`，自建的 `~/.ssh/authorized_keys` 不生效） |

各账号之间仍共享 `/tmp`，也能用 `ps` 看到别人进程的命令行，所以不要把密钥写进命令行参数。`/srv` 下已有的目录如果对其它账号可读，`install` 和 `list` 会给出警告，由目录属主执行 `chmod o-rwx /srv/<目录>` 收紧。

## 阶段 0：服务器一次性准备（管理员，需要 root 密码）

这一步只做一次，之后开项目、发邀请都不再需要 root 密码。管理员的 agent 先把文件传到服务器，再请管理员在自己的终端里执行下面的命令。agent 不收集、也不代输 sudo 密码：

```bash
scp scripts/srv_project.sh panel/srv_panel.py panel/install_panel.sh <管理员>@<服务器>:/tmp/srv-setup/
ssh -t <管理员>@<服务器> 'cd /tmp/srv-setup && sudo install -m 755 srv_project.sh /usr/local/sbin/srv-project \
  && sudo srv-project install --delegate <管理员> && sudo bash install_panel.sh --lan-ip <服务器局域网 IP> && rm -rf /tmp/srv-setup'
```

- `install` 建 `srvproj` 组和公钥目录，写好 sshd 规则并重载 sshd。
- `--delegate <管理员>` 让这个账号免密执行 `sudo srv-project`，只放行这一个命令。之后管理员的 agent 在管理员批准后，就能自动开项目、发邀请。
- **先装管理面板，再开第一个项目。** 邀请登记要经过面板。面板的首次登录（修改初始密码）由管理员本人在浏览器里完成，见 [`srv-panel.md`](srv-panel.md)。

### 有知识库的服务器

服务器上部署了知识库时，项目的日常管理（看状态、启停服务、看日志、发邀请、吊销公钥、新建和删除项目）用知识库后台的【项目运维】，不再用独立面板的网页。做法：

1. 把 `srv-project` 委托给知识库后端所在的账号：`sudo srv-project install --delegate <该账号>`。
2. 面板以登记模式运行：`sudo bash install_panel.sh --enroll-only`。此时 9300 端口只开放设备登记接口 `POST /enroll`，管理页一律 404。
3. 知识库后端通过 `sudo -n srv-project list/status/env/info/invites --json`、`logs`、`svc` 等子命令读取和操作，字段约定见 [`srv-panel.md`](srv-panel.md)。

没有知识库的服务器不受影响，面板仍以完整模式运行。

## 阶段 1：发邀请（管理员 agent，或管理员在面板上操作）

1. 先在对话里向管理员确认四件事：项目名、给谁、用哪台设备、端口。新项目的端口先查登记表和监听情况再定。管理员明确批准后才能执行，而且这次批准只对这一次邀请有效。
2. 在服务器上执行。`sudo -n` 能免密，是因为阶段 0 做了委托：

   ```bash
   ssh <管理员>@<服务器> 'sudo -n srv-project create <项目> --port <端口> --contact "<申请人>"'   # 新项目才需要
   ssh <管理员>@<服务器> 'sudo -n srv-project invite <项目> --label <申请人-设备> [--hours 24] [--from <申请人 IP>]'
   ```

   - `create` 遇到端口已被占用或已登记会拒绝。这时换一个空闲端口，告诉管理员换成了哪个，再重新执行。
   - 邀请默认 24 小时有效，只能用一次。知道申请人的 IP 时加上 `--from`，被别人截获也用不了。
3. 把输出里那一行 `srvinvite1.…` 原样交给管理员，由管理员转发给申请方，并说明「交给你的 agent」。邀请里不含任何密码或私钥，但持有它的人可以登记一次设备，所以只发给申请人本人。
4. 查看和作废邀请：`sudo -n srv-project invites <项目>`、`sudo -n srv-project invite-revoke <项目> <id>`（编号不属于该项目会报找不到）。在面板的「邀请」页也能操作。

申请方换电脑时，给同一个项目发一个新标签的邀请。新设备登记成功后，执行 `srv-project revoke <项目> <旧标签>` 吊销旧设备的公钥。

日常维护同样要在管理员批准后由 agent 执行，管理员也可以直接在面板上操作：

- `srv-project list`：查看各项目的端口、启用状态、已授权公钥和委托账号。
- `srv-project revoke <项目> <标签|SHA256:指纹>`：吊销一把公钥。吊销不会断开已建立的会话，要立即断开还需执行 `pkill -u <项目> -x sshd`。
- `srv-project disable <项目>` 停用项目，`enable <项目>` 恢复。停用后该账号不能登录，它的进程和服务全部停止，数据保留。
- `srv-project delete <项目> [--purge]`：只能删除已停用的项目。不加 `--purge` 时，项目目录归档到 `/srv/.archived/`。

## 阶段 2：登记设备（申请方 agent）

用户交来 `srvinvite1.…` 后，执行：

```bash
python <SKILL_DIR>/scripts/safe_push.py enroll '<邀请>'
```

它会依次完成以下几步，任何一步失败都会停下：

1. 在本地解析邀请。邀请格式不对或已过期时，直接报错，不发任何网络请求。
2. 检查本机的 `~/.ssh/known_hosts` 和 `~/.ssh/config`。如果已有同一服务器同一类型但内容不同的主机公钥，或者已有同名但内容不同的别名，就停下来报告。主机公钥对不上，可能是中间人攻击，也可能是服务器重装过，交给用户和管理员核实，不自行覆盖。这一步在提交之前，所以出现冲突时邀请不会被用掉。
3. 生成这个项目专用的密钥 `~/.ssh/id_ed25519_srv_<项目>`，已有就沿用。密钥不设密码短语，这样 agent 才能无人值守地部署。私钥只留在本机，不发出、不进仓库。
4. 连接面板，用邀请里的证书指纹校验面板证书。指纹不一致就断开，邀请码不会被发出。校验通过后，凭邀请码提交公钥，由面板完成授权。
5. 用邀请里的主机公钥写入 `known_hosts`，在 `~/.ssh/config` 里写入别名 `srv-<项目>`，再用这个别名验证 SSH 登录。
6. 打印接入信息，以及 `config-save` 用的配置样例。

邀请码无效、过期或已用过时，请用户找管理员重新发邀请，不要反复重试，连续失败会被锁定。

## 阶段 3：首次部署（申请方 agent）

登记成功后，按顺序执行下面的步骤，每一步都核对结果。

1. 用 `enroll` 打印的样例登记部署配置，然后执行 `config-save`：
   - `server.host` 填 `srv-<项目>`，`user` 填项目账号，`remote_repo_dir` 填 `/srv/<项目>/repo`；
   - 依赖目录、构建产物、`.env` 等写进 `runtime_paths`，并且这些路径必须已被 `.gitignore` 忽略。
2. 运行时和端口要求以项目的声明为准，比如 `package.json` 里的 `engines`、`.nvmrc`、`requirements.txt`；端口用接入信息里登记的那个。
3. 首次推送代码：先执行 `safe_push.py bootstrap --repo <仓库>` 预检，再加 `--execute` 正式执行。它会把当前分支克隆进空的 `repo/`。
4. 运行时装在项目账号自己的家目录里。以 Node 为例（版本按项目要求，`linux-x64` 按服务器架构）：

   ```bash
   V=v22.23.3
   curl -fL https://npmmirror.com/mirrors/node/$V/node-$V-linux-x64.tar.xz | tar -xJ -C ~/.local
   ln -sfn ~/.local/node-$V-linux-x64 ~/.local/node
   grep -q '.local/node/bin' ~/.profile || echo 'export PATH="$HOME/.local/node/bin:$PATH"' >> ~/.profile
   ```

   服务器要走代理才能访问 GitHub 时，安装依赖前设置 `https_proxy`。`better-sqlite3` 这类原生模块的预编译包从 GitHub 下载，不设代理就会退回到本地编译。
5. 装依赖、构建，然后写 `~/config/.env`（`umask 027`，权限 640）。
   - 端口用登记的端口。
   - 密钥类的值和应用初始密码一律随机生成（`openssl rand`），不沿用示例里的默认值。
   - 数据库、上传目录指向 `~/data/`。
   - 含空格的值加双引号，这样 systemd 和 shell 都能读。
   需要初始化数据库或建管理员的，在这一步执行。
6. 配 systemd 用户服务（`~/.config/systemd/user/<服务>.service`）：

   ```ini
   [Unit]
   Description=<项目>

   [Service]
   WorkingDirectory=%h/repo/<后端目录>
   EnvironmentFile=%h/config/.env
   ExecStart=%h/.local/node/bin/node <入口>
   Restart=on-failure

   [Install]
   WantedBy=default.target
   ```

   然后执行 `systemctl --user daemon-reload && systemctl --user enable --now <服务>`。日志用 `journalctl --user -u <服务> -n 100` 查看。
7. 在仓库里加两个脚本并提交，加好后补进部署配置：
   - post-sync 脚本：装依赖、构建、执行 `systemctl --user restart <服务>`；
   - health 脚本：请求应用的健康检查接口。
   以后每次更新都用 `safe_push.py preflight --remote` 和 `deploy --execute`，状态为 `verified` 才算完成。
8. 交付给用户：
   - 访问地址 `http://<服务器>:<端口>`；
   - 应用的初始账号和密码，从服务器上读取：`ssh srv-<项目> 'grep INITIAL_ADMIN_PASSWORD ~/config/.env'`。只在对话里交给用户本人，不写进任何文件；
   - 提醒用户首次登录后在应用里改密码。

## 备用：不经面板的申请制

面板不可用时（比如还没装、或者申请方连不上面板端口），走手工流程：

1. 申请方的 agent 生成密钥 `ssh-keygen -t ed25519 -N '' -C '<申请人>@<项目>' -f ~/.ssh/id_ed25519_srv_<项目>`，然后写一份申请交给用户转发，内容包括项目名、申请人、期望端口、运行时、公钥整行和指纹。
2. 管理员的 agent 向管理员复述这些要点，管理员批准后执行。如果是新项目，先按阶段 1 执行 `create`：

   ```bash
   ssh <管理员>@<服务器> 'sudo -n srv-project grant <项目> - --label <标签>' < 公钥行
   ssh <管理员>@<服务器> 'sudo -n srv-project info <项目>'
   ```

   再把 `info` 的输出交给管理员转发给申请方。公钥和指纹走的是同一个渠道，被调包时指纹也会一起被换。渠道不可信时，请管理员当面或电话向申请人核对指纹。
3. 申请方的 agent 手动写 `~/.ssh/config` 别名。第一次连接时，请用户确认服务器的主机指纹（本技能不会自动接受），之后从阶段 3 继续。

## 应用的初始账号密码

- 初始密码只存在 `/srv/<项目>/config/.env` 里。能读到它的只有两方：项目账号（即申请方 agent），以及项目组里的服务器管理员。
- 不进 git，不写进部署文档，不打印到日志，不在管理员和申请方之间转述。
- `.env` 里的初始值只在首次建库时使用。用户在应用里改了密码之后，再改这个值不会影响已有账号。
