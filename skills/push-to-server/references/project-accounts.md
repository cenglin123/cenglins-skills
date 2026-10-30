# 共享服务器的项目账号与授权

一台服务器给多个人或多个 agent 部署项目时，每个 `/srv/<项目>` 配一个独立的 Linux 账号。服务器 root 用 `scripts/srv_project.sh` 开账号、授权公钥、停用和吊销；对方的 agent 拿到授权后，只能运维自己那个目录，用本技能的 `safe_push.py` 推送代码。

## 账号的权限边界

| 能做 | 不能做 |
|---|---|
| 读写 `/srv/<项目>`（家目录，750） | 读写其它项目、管理员家目录、`/srv` 根目录 |
| 用 `systemctl --user` 管自己的服务，登出后照常运行（linger） | `sudo`、加入 `docker` 组 |
| 在自己目录里装运行时（如 `~/.local/node`） | 密码登录、端口转发、agent 转发、X11 |
| 监听登记的端口（≥1024） | 改授权公钥（在 root 所有的 `/etc/ssh/srv-keys/<账号>`，自建的 `~/.ssh/authorized_keys` 不生效） |

已有的 `/srv` 目录如果对其它账号可读，项目账号能看到其中内容；`install` 和 `list` 会警告，属主执行 `chmod o-rwx /srv/<目录>` 收紧。账号间仍共享 `/tmp`、能用 `ps` 看到别人的进程命令行，别把密钥写在命令行参数里。

## 服务器 root 的操作

脚本装到服务器上：

```bash
scp scripts/srv_project.sh <服务器>:/tmp/ && ssh -t <服务器> 'sudo install -m 755 /tmp/srv_project.sh /usr/local/sbin/srv-project && rm /tmp/srv_project.sh'
```

依次执行（都要 `sudo`）：

1. `srv-project install`：一次性。建 `srvproj` 组和公钥目录，写 sshd 规则并重载 sshd。
2. 部署管理面板，见 [`srv-panel.md`](srv-panel.md)。之后的查看和日常操作可以在面板上做。
3. `srv-project create <项目> --port <端口> --contact <联系人>`：开账号、建 `repo/ config/ data/ backups/`、开 linger、在 `/srv/PROJECTS.tsv` 登记端口。端口已被占用或已登记会拒绝。执行人（`SUDO_USER`）会被加进项目组，可以只读查看项目目录。
4. `srv-project grant <项目> <对方公钥.pub> --label <标签> [--from <来源地址>]`：只收公钥（一行），私钥由对方自己保管。`--from` 限定来源 IP 或网段。
5. 把下一节的信息发给对方。

日常维护：

- `srv-project list`：看各项目的端口、启用状态和已授权公钥。
- `srv-project revoke <项目> <标签|SHA256:指纹>`：吊销一把公钥。已连着的会话要立即断开，用 `pkill -u <项目> -x sshd`。
- `srv-project disable <项目>`：停用账号。登录被拒，该账号的进程和服务全部停止，数据不动。`enable` 恢复，已 enable 的用户服务会自动起来。
- 删除项目没有命令。先 `disable`，确认数据已备份或不要了，再手动 `userdel <项目>`，删 `/etc/ssh/srv-keys/<项目>`、`/srv/<项目>` 和登记表中的那一行。

## 发给对方 agent 的信息

```text
服务器：<主机>，SSH 端口 22
账号：<项目>（只认你提供的公钥，没有密码）
项目目录：/srv/<项目>/{repo,config,data,backups}
服务端口：<端口>（只用这个端口，监听 0.0.0.0:<端口>）
权限：只能写自己的目录；没有 sudo；用 systemctl --user 管服务
```

## 对方 agent 的首次部署

`safe_push.py` 不初始化远端仓库，第一次用 bundle 克隆进空的 `repo/`：

```bash
git bundle create /tmp/seed.bundle HEAD <分支>
scp /tmp/seed.bundle <项目>@<主机>:/tmp/seed.bundle
ssh <项目>@<主机> 'git clone /tmp/seed.bundle ~/repo && rm /tmp/seed.bundle'
```

再在本地登记配置（`remote_repo_dir` 填 `/srv/<项目>/repo`，`user` 填项目账号），以后每次用 `preflight --remote`、`deploy --execute` 推送。

服务用 systemd 用户单元，放在 `~/.config/systemd/user/<服务>.service`。配置和数据引用同级的 `config/`、`data/`：

```ini
[Unit]
Description=<项目>

[Service]
WorkingDirectory=%h/repo/backend
EnvironmentFile=%h/config/.env
ExecStart=%h/.local/node/bin/node src/app.js
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now <服务>
journalctl --user -u <服务> -n 100
```

部署后要重启服务的，把 `systemctl --user restart <服务>` 写进仓库里的 post-sync 脚本。
