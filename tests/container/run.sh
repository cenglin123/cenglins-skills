#!/usr/bin/env bash
# 容器端到端：一次性 ubuntu:24.04 + systemd 容器里跑 srv_project.sh / srv_panel.py 的外部服务与迁入全流程
# （含委托账号越权负例、create --takeover 失败注入回滚、无 EXTERNAL.tsv 时 delete、端口前导 0、登记表并发）。
# 不是 pytest 用例：需要本机有可用的 docker（Linux 容器）。不发布任何端口，跑完删除容器与镜像。
#   bash tests/container/run.sh
set -euo pipefail
# MSYS_NO_PATHCONV 保住容器内路径不被 Git Bash 改写成 Windows 路径；
# 代价是宿主机路径也不再自动转换，所以 HERE/SKILL（docker build / cp 的宿主机侧）用 Windows 风格路径。
export MSYS_NO_PATHCONV=1
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && { pwd -W 2>/dev/null || pwd; })
SKILL=${SKILL_DIR:-$HERE/../../skills/push-to-server}
NAME=srvext-e2e
IMAGE=srvext-e2e:1

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker rmi "$IMAGE" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker build -q -t "$IMAGE" "$HERE" >/dev/null
docker run -d --name "$NAME" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw "$IMAGE" >/dev/null
sleep 6
docker cp "$SKILL/scripts/srv_project.sh" "$NAME:/tmp/srv-project"
docker cp "$SKILL/panel/srv_panel.py" "$NAME:/tmp/srv_panel.py"
docker cp "$SKILL/panel/install_panel.sh" "$NAME:/tmp/install_panel.sh"
docker cp "$HERE/setup.sh" "$NAME:/tmp/setup.sh"
docker cp "$HERE/e2e.sh" "$NAME:/tmp/e2e.sh"
docker exec "$NAME" sed -i 's/\r$//' /tmp/setup.sh /tmp/e2e.sh
docker exec "$NAME" bash /tmp/setup.sh >/dev/null
docker exec "$NAME" bash /tmp/e2e.sh | tee /dev/stderr | tail -1 | grep -q 'fail=0'
