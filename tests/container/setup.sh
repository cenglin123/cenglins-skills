#!/bin/bash
set -e
sed 's/\r$//' /tmp/srv-project >/usr/local/sbin/srv-project
chmod 755 /usr/local/sbin/srv-project
mkdir -p /tmp/panel
sed 's/\r$//' /tmp/srv_panel.py >/tmp/panel/srv_panel.py
sed 's/\r$//' /tmp/install_panel.sh >/tmp/panel/install_panel.sh
id admin >/dev/null 2>&1 || useradd -m -s /bin/bash admin
id otheru >/dev/null 2>&1 || useradd -m -s /bin/bash otheru
mkdir -p /run/sshd
[ -f /srv/PROJECTS.tsv ] || srv-project install --delegate admin
echo "--- registry header"
cat -A /srv/PROJECTS.tsv | sed 's/M-[^$^]*//g'
echo "--- 安装面板（登记模式，用新 srv_panel.py）"
IP=$(hostname -I | awk '{print $1}')
bash /tmp/panel/install_panel.sh --enroll-only --lan-ip "$IP" 2>&1 | tail -4
cmp /tmp/panel/srv_panel.py /usr/local/lib/srv-panel/srv_panel.py && echo "面板代码已是新版"
echo "--- usage"
srv-project 2>&1 | tail -14 || true
