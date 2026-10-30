# /srv 项目管理面板

给服务器管理员（root）用的网页面板，管理 `srv-project`（`scripts/srv_project.sh`）创建的 `/srv/<项目>` 项目账号。界面为中文服务端渲染网页，单文件、只用 Python 3.12 标准库，无外部 JS/CSS。

文件：

- `panel/srv_panel.py`：面板本体
- `panel/install_panel.sh`：安装脚本（root 运行，可重复执行）

## 安装

前提：服务器已安装 `/usr/local/sbin/srv-project` 并执行过 `srv-project install`。

```bash
sudo bash panel/install_panel.sh
```

脚本会：

1. 把 `srv_panel.py` 装到 `/usr/local/lib/srv-panel/`
2. 令牌文件 `/etc/srv-panel/token` 不存在时生成（随机 48 位十六进制，权限 600，属 root；已有则沿用，不打印）
3. 写 `/etc/systemd/system/srv-panel.service`（root 运行，`Restart=on-failure`，只绑 `127.0.0.1:9300`），`enable` 并启动

重复执行即升级面板代码并重启服务，令牌不变。

## 访问

面板只监听服务器本机回环，用 SSH 端口转发访问：

1. 管理员在自己电脑上：`ssh -L 9300:127.0.0.1:9300 <服务器>`
2. 浏览器打开 `http://127.0.0.1:9300`
3. 令牌在服务器上查看：`sudo cat /etc/srv-panel/token`

## 功能

**总览**（`/`）：每个项目一张卡片

- 启用 / 停用状态（`/etc/shadow` 第 8 字段为 1 即停用）
- 登记端口是否在监听、监听进程属主是否为该项目账号
- 问题提示：登记端口被其他账号占用；两个项目登记同一端口；账号缺失；账号不在 `srvproj` 组
- 已授权公钥（标签 + 指纹 + 来源限制）
- 用户服务及状态（`systemctl --user --machine=<账号>@ list-units`；只显示该项目自己的服务，隐藏 `dbus.service` 和无单元文件的 inactive 系统单元）
- `repo/` 的 git HEAD、`/srv/<项目>` 磁盘占用

**项目详情**（`/project/<项目>`）：在总览基础上提供全部操作，并做环境依赖检测

- 环境依赖：以该账号身份运行 `runuser -l <账号> -c "bash -lic …"`（登录加交互 shell，因此 `~/.profile`、`~/.bashrc` 里加的 PATH，例如 `~/.local/node/bin`，也会生效），报告 git、node、npm、python3、docker 的可用性、版本和路径
- 声明比对：`package.json` 的 `engines.node`、`.nvmrc`、`requirements.txt`、`pyproject.toml`（含 `requires-python`）与实际版本是否匹配。版本约束支持 `>= > <= < = ^ ~ x` 范围和 `||`，看不懂的约束标为「无法自动判断」而不是误报
- 停用的项目账号被锁定，不做环境检测

**全局检查**（`/global`）：`srv-project`、sshd 规则、公钥目录、登记表是否在；`/srv` 下对 other 可读的目录（警告）；`/srv` 下未登记的目录；本机所有监听端口及属主，标出登记的项目端口与未登记端口。

**操作**：新建项目、授权公钥（粘贴单行公钥，经 stdin 走 `grant <项目> -`）、吊销、停用、启用、用户服务 restart / start / stop、查看用户服务最近 100 行日志（`journalctl _UID=<uid> _SYSTEMD_USER_UNIT=<单元> + _UID=<uid> USER_UNIT=<单元>`）。

- 停用、吊销、停止服务先进入确认页，点「确认执行」才真正执行（页面内表单，不用 JS `confirm()`）
- `srv-project` 或 `systemctl` 的命令、退出码、标准输出、标准错误原样回显到结果页

## 安全模型

- 必须 root 启动，否则退出；令牌文件须属 root 且权限 600，否则拒绝启动
- 默认只绑 `127.0.0.1`。绑非回环地址必须显式 `--allow-remote`，并用 `--allowed-host <主机名>` 补充允许的 Host。不建议开启，没有 TLS
- 登录：令牌常量时间比较，成功后换 `HttpOnly; SameSite=Strict` 会话 cookie（8 小时，保存在进程内存，重启即失效）；失败后延迟 1 秒，一分钟内失败 10 次即暂时拒绝
- 所有 POST 带 CSRF 令牌（与会话绑定的 HMAC），缺失或不对返回 403
- Host 头只接受 `127.0.0.1` / `localhost`（可带本服务端口），防 DNS rebinding
- 输出全部 `html.escape`；响应带 CSP（无脚本）、`X-Frame-Options: DENY`、`no-store`
- 项目名用与 `srv_project.sh` 相同的正则校验，并要求已登记；服务名、标签、来源地址、公钥引用均有格式白名单；变更操作只通过参数列表调用 `srv-project` / `systemctl`，不经 shell
- 项目目录内容由项目账号控制，所以：读 `repo/` 的版本声明文件只读真实路径仍在 `/srv/<项目>/` 内的小普通文件；读 git HEAD 用 `runuser -u <账号>` 降权运行，避免 root 执行项目仓库里可触发命令的 git 配置
- 审计日志 `/var/log/srv-panel.log`（权限 600）：时间、动作、参数、来源、退出码。授权公钥只记标签、来源限制和指纹；不记令牌，也不记公钥全文

## 参数

```
srv_panel.py [--bind 127.0.0.1] [--port 9300] [--allow-remote]
             [--allowed-host 主机名]... [--token-file /etc/srv-panel/token]
             [--log-file /var/log/srv-panel.log]
```

## 已知限制

- 会话保存在内存，面板重启后需要重新登录
- 总览每次加载会为每个项目执行 `systemctl`、`ssh-keygen`、`runuser git`、`du`，项目很多或目录很大时较慢
- 用户服务的日志依赖 journal 中带 `_UID` 的记录；服务自身 stdout 是否落入 journal 取决于服务单元配置
- 面板不删除项目（`srv-project` 没有 delete 子命令）
- `grant` 的 `--from` 与吊销时的「标签」仍由 `srv-project` 做最终校验，错误原文回显

## 卸载

```bash
sudo systemctl disable --now srv-panel.service
sudo rm -f /etc/systemd/system/srv-panel.service
sudo systemctl daemon-reload
sudo rm -rf /usr/local/lib/srv-panel
sudo rm -rf /etc/srv-panel        # 删除令牌
sudo rm -f /var/log/srv-panel.log # 可选：删除审计日志
```

卸载面板不影响任何项目账号、公钥和用户服务。
