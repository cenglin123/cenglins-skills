FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv dbus dbus-user-session libpam-systemd sudo openssh-server python3 iproute2 openssl procps passwd ca-certificates && apt-get clean
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
