#!/bin/sh
# 环境探针:在 alpine 容器内一次性运行,stdout 输出 KEY=VALUE 供宿主入口解析。
# 以容器视角探测 Docker 引擎所在的 Linux 环境(WSL2 VM 或原生 Linux)。
# 可选挂载 /cache:存在时把 /proc/config.gz 存为 /cache/kernel/config-running,
# 作为后续内核编译的 config 基线(必须趁旧内核还在运行时采集)。
# 可选 env:PROBE_PROXY —— 探测该代理是否能从容器内达到 api.telegram.org。
set -eu

arch="$(uname -m)"
case "$arch" in
  x86_64) arch=amd64 ;;
  aarch64) arch=arm64 ;;
esac

kernel="$(uname -r)"
case "$kernel" in
  *-microsoft-standard-WSL2*) wsl2=true ;;
  *) wsl2=false ;;
esac

# binder 双通道:binderfs 在 /proc/filesystems 注册名为 "binder";
# legacy 字符设备通道(BINDER_IPC=y 而无 BINDERFS)出现在 /proc/misc。
binderfs=false
binder_misc=false
grep -qw binder /proc/filesystems 2>/dev/null && binderfs=true
grep -qw binder /proc/misc 2>/dev/null && binder_misc=true
binder_ok=false
{ [ "$binderfs" = true ] || [ "$binder_misc" = true ]; } && binder_ok=true

ashmem=false
grep -qw ashmem /proc/misc 2>/dev/null && ashmem=true
[ -e /dev/ashmem ] && ashmem=true

gpu=false
[ -e /dev/dri ] && gpu=true

if [ -d /cache ] && [ -r /proc/config.gz ]; then
  mkdir -p /cache/kernel
  zcat /proc/config.gz > /cache/kernel/config-running 2>/dev/null || true
fi

proxy_ok=false
if [ -n "${PROBE_PROXY:-}" ]; then
  if command -v wget >/dev/null 2>&1; then
    http_proxy="$PROBE_PROXY" https_proxy="$PROBE_PROXY" \
      wget -q -T 8 -O /dev/null https://api.telegram.org/ 2>/dev/null && proxy_ok=true
  fi
fi

echo "ARCH=$arch"
echo "KERNEL=$kernel"
echo "WSL2=$wsl2"
echo "BINDERFS=$binderfs"
echo "BINDER_MISC=$binder_misc"
echo "BINDER_OK=$binder_ok"
echo "ASHMEM=$ashmem"
echo "GPU=$gpu"
echo "PROXY_OK=$proxy_ok"
