# 共享 /srv 服务器的项目接入

一台服务器给多个人部署项目时，每个 `/srv/<项目>` 配一个独立的 Linux 账号。整个接入由双方的 agent 完成，人只做三件事：

| 角色 | 人做的事 | agent 做的事 |
|---|---|---|
| 申请方（局域网用户） | 把 agent 写好的申请转给管理员；收到接入信息后转给 agent；首次登录应用后改密码 | 生成密钥、写申请；按接入信息首次部署、配服务、交付访问地址和初始密码 |
| 服务器管理员 | 说「批准」 | 核对申请、开账号、授权公钥、生成接入信息 |

按角色执行下面的阶段，不要跨角色代办：申请方 agent 不碰服务器 root，管理员 agent 不替申请方部署应用。

## 账号的权限边界

| 能做 | 不能做 |
|---|---|
| 读写 `/srv/<项目>`（家目录，750） | 读写其它项目、管理员家目录、`/srv` 根目录 |
| 用 `systemctl --user` 管自己的服务，登出后照常运行（linger） | `sudo`、加入 `docker` 组 |
| 在自己目录里装运行时（如 `~/.local/node`） | 密码登录、端口转发、agent 转发、X11 |
| 监听登记的端口（≥1024） | 改授权公钥（在 root 所有的 `/etc/ssh/srv-keys/<账号>`，自建的 `~/.ssh/authorized_keys` 不生效） |

账号间仍共享 `/tmp`，也能用 `ps` 看到别人的进程命令行，别把密钥写进命令行参数。已有的 `/srv` 目录如果对其它账号可读，`install` 和 `list` 会警告，属主执行 `chmod o-rwx /srv/<目录>` 收紧。

## 阶段 0：服务器一次性准备（管理员，需要 root 密码）

只做一次，后续接入都不再需要 root 密码。管理员 agent 把文件传到服务器，再请管理员在自己的终端里执行（agent 不收集、不代输 sudo 密码）：

```bash
scp scripts/srv_project.sh panel/srv_panel.py panel/install_panel.sh <管理员>@<服务器>:/tmp/srv-setup/
ssh -t <管理员>@<服务器> 'cd /tmp/srv-setup && sudo install -m 755 srv_project.sh /usr/local/sbin/srv-project \
  && sudo srv-project install --delegate <管理员> && sudo bash install_panel.sh && rm -rf /tmp/srv-setup'
```

- `install` 建 `srvproj` 组和公钥目录，写 sshd 规则并重载。
- `--delegate <管理员>` 允许这个账号免密执行 `sudo srv-project`，而且只放行这一个命令。之后管理员 agent 就能在管理员批准后自动开通项目。
- **先装管理面板，再开第一个项目。** 面板的首次登录（改初始密码）由管理员本人在浏览器里完成，见 [`srv-panel.md`](srv-panel.md)。

## 阶段 1：申请（申请方 agent）

1. 生成这个项目专用的密钥，不复用个人的默认密钥。已存在就沿用：

   ```bash
   ssh-keygen -t ed25519 -N '' -C '<申请人>@<项目>' -f ~/.ssh/id_ed25519_srv_<项目>
   ssh-keygen -lf ~/.ssh/id_ed25519_srv_<项目>.pub
   ```

   没有密码短语，是为了让 agent 能无人值守地部署。私钥只留在本机，绝不发出、不进仓库。
2. 从项目里确定项目名、运行时和期望端口。项目名用小写字母、数字、`-`、`_`。运行时以 `package.json` 的 `engines`、`.nvmrc`、`requirements.txt` 等为准。
3. 生成申请，交给用户转发给管理员：

   ```text
   【/srv 项目接入申请】
   项目名：<项目>
   申请人：<姓名或联系方式>
   期望端口：<端口>（≥1024，服务器上已占用时由管理员另分配）
   运行时：<如 Node 22 / Python 3.12>
   公钥：<~/.ssh/id_ed25519_srv_<项目>.pub 的整行内容>
   公钥指纹：<SHA256:…>
   ```

## 阶段 2：审批与授权（管理员 agent）

1. 收到申请后，先在对话里向管理员复述要点：项目名、申请人、端口、指纹。等管理员明确批准后再执行，批准只对这一份申请有效。
   - 公钥和指纹是从同一渠道传来的，被调包时指纹也会一起被换。渠道不可信时，请管理员另外向申请人口头核对指纹。
2. 在服务器上执行（`sudo -n` 由阶段 0 的委托放行）。公钥通过标准输入传，不落盘：

   ```bash
   ssh <管理员>@<服务器> 'sudo -n srv-project create <项目> --port <端口> --contact "<申请人>"'
   ssh <管理员>@<服务器> 'sudo -n srv-project grant <项目> - --label <申请人或机器名>' < 申请里的公钥行
   ssh <管理员>@<服务器> 'sudo -n srv-project info <项目>'
   ```

   - 端口被占用或已登记时，`create` 会拒绝。另选一个空闲端口，告诉管理员换了哪个，然后再执行。
   - 核对 `grant` 输出的指纹和申请里的一致，不一致就停下，交给管理员处理。
3. 把 `info` 的输出原样交给管理员，由管理员发给申请方。接入信息里不含密码和私钥。

日常维护同样由管理员 agent 在批准后执行，管理员也可以直接在面板上操作：

- `srv-project list`：各项目的端口、启用状态、已授权公钥、委托账号。
- `srv-project grant` 加新公钥，或 `srv-project revoke <项目> <标签|SHA256:指纹>` 吊销旧公钥：用于申请方换电脑。吊销后，已连着的会话要另外用 `pkill -u <项目> -x sshd` 断开。
- `srv-project disable <项目>` 停用、`enable <项目>` 恢复。停用后账号登录被拒，该账号的进程和服务全部停止，数据保留。
- `srv-project delete <项目> [--purge]`：只能删除已停用的项目。不加 `--purge` 时，数据归档到 `/srv/.archived/`。

## 阶段 3：首次部署（申请方 agent）

拿到接入信息后，按下面的顺序执行，每一步都核对结果。

1. 在 `~/.ssh/config` 里加一个主机别名，后面的命令都用这个别名：

   ```text
   Host srv-<项目>
     HostName <服务器>
     Port <SSH 端口>
     User <项目>
     IdentityFile ~/.ssh/id_ed25519_srv_<项目>
     IdentitiesOnly yes
   ```

   第一次连接时，请用户确认主机指纹（本技能不会自动接受 host key）。然后执行 `ssh srv-<项目> 'id; ls ~'` 验证登录。
2. 登记部署配置并保存（`config-save`）：
   - `server.host` 填 `srv-<项目>`，`user` 填项目账号，`remote_repo_dir` 填 `/srv/<项目>/repo`；
   - 依赖目录、构建产物、`.env` 等写进 `runtime_paths`，并且必须已被 `.gitignore` 忽略。
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

## 应用的初始账号密码

- 初始密码只存在 `/srv/<项目>/config/.env` 里。能读到它的只有两方：项目账号（即申请方 agent），以及项目组里的服务器管理员。
- 不进 git，不写进部署文档，不打印到日志，不在管理员和申请方之间转述。
- `.env` 里的初始值只在首次建库时使用。用户在应用里改了密码之后，再改这个值不会影响已有账号。
