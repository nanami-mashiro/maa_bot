#!/bin/bash
# 在 debian 容器内编译带 binder/binderfs 的 WSL2 内核。
# 用法(容器内): build-wsl-kernel.sh <kernel-version>
#   <kernel-version> 形如 6.18.33.2(已去掉 -microsoft-standard-WSL2 后缀)。
# 挂载:
#   /cache  读写。产物 /cache/kernel/bzImage-<ver>-binder;
#           优先使用 /cache/kernel/config-running(probe 阶段从运行内核采集)作 config 基线,
#           保证 LOCALVERSION 等与官方逐字节一致;缺失时回退仓库内 Microsoft/config-wsl。
#           源码 tarball 也缓存于 /cache/kernel/。
# 环境变量: HTTP_PROXY/HTTPS_PROXY 可选,GitHub 访问优先走代理,失败回退直连。
# 注意:编译在容器文件系统内进行(避开挂载盘 IO),只把产物拷回 /cache。
#       源码用 tarball 而非 git clone:无 .git 则 setlocalversion 不会追加 -g<sha>,
#       uname -r 与微软官方一致,存量 modules VHD 目录名/vermagic 才能命中。
set -euo pipefail

VER="${1:?usage: build-wsl-kernel.sh <kernel-version>}"
REPO_URL="https://github.com/microsoft/WSL2-Linux-Kernel"
OUT="/cache/kernel/bzImage-${VER}-binder"
mkdir -p /cache/kernel

log() { echo "[build-wsl-kernel] $*"; }

curl_pf() {
  # 代理优先、直连回退。
  if [ -n "${HTTPS_PROXY:-}" ]; then
    curl -fL --retry 2 -x "$HTTPS_PROXY" "$@" && return 0
    log "代理访问失败,回退直连"
  fi
  curl -fL --retry 2 "$@"
}

export DEBIAN_FRONTEND=noninteractive
log "安装编译依赖"
apt-get update -qq >/dev/null
apt-get install -y -qq build-essential flex bison libssl-dev libelf-dev bc \
  python3 cpio curl git ca-certificates xz-utils kmod dwarves >/dev/null

# tag 解析:精确匹配当前版本;缺失时同 major.minor 内取 <=当前 的最大版本,
# 再没有则取最新。用 git ls-remote(免 API rate limit,天然可走代理)。
git_lsremote() {
  if [ -n "${HTTPS_PROXY:-}" ]; then
    git -c "http.proxy=${HTTPS_PROXY}" ls-remote --tags "$REPO_URL" "$1" 2>/dev/null && return 0
  fi
  git ls-remote --tags "$REPO_URL" "$1"
}

TAG="linux-msft-wsl-${VER}"
if ! git_lsremote "refs/tags/${TAG}" | grep -q .; then
  MM="$(echo "$VER" | cut -d. -f1-2)"
  log "tag ${TAG} 不存在,在 linux-msft-wsl-${MM}.* 中就近选择"
  ALL="$(git_lsremote "refs/tags/linux-msft-wsl-${MM}.*" \
    | awk '{print $2}' | sed 's#refs/tags/##;s#\^{}##' | sort -uV)"
  TAG="$(printf '%s\nlinux-msft-wsl-%s\n' "$ALL" "$VER" | sort -V \
    | grep -B999 -x "linux-msft-wsl-${VER}" | grep -v -x "linux-msft-wsl-${VER}" | tail -1 || true)"
  [ -z "$TAG" ] && TAG="$(printf '%s\n' "$ALL" | head -1 || true)"
  if [ -z "$TAG" ]; then
    log "错误:找不到与内核 ${VER} 匹配的微软内核 tag"
    exit 1
  fi
  log "使用兜底 tag: ${TAG}(与运行内核版本不一致,uname 将变化,可加载模块可能失效)"
  echo "KERNEL_TAG_MISMATCH=true"
fi

TARBALL="/cache/kernel/${TAG}.tar.gz"
if [ ! -s "$TARBALL" ]; then
  log "下载内核源码 ${TAG}"
  curl_pf -o "${TARBALL}.part" "${REPO_URL}/archive/refs/tags/${TAG}.tar.gz"
  mv "${TARBALL}.part" "$TARBALL"
else
  log "命中源码缓存 ${TARBALL}"
fi

mkdir -p /src
tar -xzf "$TARBALL" -C /src --strip-components=1
cd /src

log "配置内核(基线 + binder/binderfs + 关键子系统内建)"
if [ -s /cache/kernel/config-running ]; then
  cp /cache/kernel/config-running .config
  log "使用运行内核的 /proc/config.gz 作为基线"
else
  cp Microsoft/config-wsl .config
  log "警告:无 config-running,回退仓库 Microsoft/config-wsl"
fi
# 内核裁剪(踩坑教训):
# - config-wsl 与 docker-desktop 运行内核 config 完全一致,base 用哪个都行。
# - 自编内核换掉官方内核后,docker-desktop VM 内无配套 modules(modules.dep 缺失),
#   官方预编译 .ko 加载不了 → dockerd 建网络失败 → Docker Desktop 反复重启 distro。
#   因此 Docker 必需的网络栈必须内建进内核,不能依赖可加载模块。
# - 引导挂载 erofs 根镜像同理需要 EROFS_FS=y。
# - wsl-bootstrap 引导早期还要挂 CLI 工具 ISO;官方 config 里 ISO9660_FS=m
#   (模块),自编内核加载不了 .ko → "unknown filesystem type 'iso9660'" 死循环。
#   (首次切换内核踩出,2026-09-03)
# - 但"把 ~30 个 netfilter/vsock/vhost 全部 =y"会触发 WSL2 CreateVm E_ABORT
#   (疑似 vsock/vhost 或 ipv6 某项破坏引导);改用 Docker 官方推荐的精简内建集。
# - 原则:凡 wsl-bootstrap/dockerd 早期要用而这台 VM 里加载不了的 fs/net,
#   一律内建;后续再报 "unknown filesystem type X" 就补 X,不要一次塞满。
BUILTIN="
EROFS_FS
ISO9660_FS
BRIDGE BRIDGE_NETFILTER TUN VETH
NF_CONNTRACK NF_NAT
IP_NF_IPTABLES IP_NF_FILTER IP_NF_NAT
NETFILTER_XT_MATCH_ADDRTYPE NETFILTER_XT_MATCH_CONNTRACK
NETFILTER_XT_TARGET_MASQUERADE NETFILTER_XT_NAT
NETFILTER_XT_MARK NF_NAT_MASQUERADE
NF_REJECT_IPV4 IP_NF_TARGET_REJECT
IP_NF_IPTABLES_LEGACY
NFT_COMPAT NFT_REJECT NFT_REJECT_IPV4
IP6_NF_IPTABLES_LEGACY IP6_NF_IPTABLES IP6_NF_FILTER IP6_NF_NAT
NF_REJECT_IPV6 IP6_NF_TARGET_REJECT NFT_REJECT_IPV6
"
# NF_REJECT_IPV4/IP_NF_TARGET_REJECT:Docker 引擎 services netns 初始化
# 要下 -j REJECT 规则,缺内建则 iptables exit 4 → 引擎起不来(2026-09-03 踩出)。
# IP_NF_IPTABLES_LEGACY:6.x 起 IP_NF_FILTER/IP_NF_NAT 依赖它;缺则 olddefconfig
# 把它们静默降回 =m(--enable 白设)。NFT_COMPAT/NFT_REJECT*:Docker Desktop 的
# iptables 是 nft 后端,xt 扩展经 compat 桥、REJECT 有原生 nft 路径,都须内建。
# DEBUG_INFO_BTF 必须保留(基线已有):Docker Desktop 的 oom tracer 用
# eBPF CO-RE,内核无 BTF 则引擎起不来(2026-09-03 踩出)。编译依赖 pahole(dwarves)。
set -- --enable ANDROID_BINDER_IPC --enable ANDROID_BINDERFS \
  --set-str ANDROID_BINDER_DEVICES "binder,hwbinder,vndbinder" \
  --enable DEBUG_INFO_BTF
for opt in $BUILTIN; do set -- "$@" --enable "$opt"; done
scripts/config "$@"
make olddefconfig >/dev/null
grep -E "^CONFIG_ANDROID_BINDER" .config
# 硬校验:olddefconfig 会按依赖上限把 =y 静默降回 =m(踩过 IP_NF_FILTER),
# 逐项确认内建真的生效,缺谁报谁。
MISSING=""
for opt in ANDROID_BINDER_IPC ANDROID_BINDERFS $BUILTIN; do
  grep -qx "CONFIG_${opt}=y" .config || MISSING="$MISSING $opt"
done
if [ -n "$MISSING" ]; then
  log "错误:以下项未能内建(=y),多半是依赖缺失被 olddefconfig 降级:$MISSING"
  for opt in $MISSING; do grep "CONFIG_${opt}[=\" ]" .config || echo "CONFIG_${opt} 不存在"; done
  exit 1
fi

log "编译 bzImage(-j$(nproc),预计数分钟到数十分钟)"
make -j"$(nproc)" bzImage 2>&1 | tail -3
sz=$(stat -c%s arch/x86/boot/bzImage)
log "bzImage 体积: $((sz/1024/1024))MB"
# WSL2 对超大内核会在 CreateVm 阶段 E_ABORT;官方约 15MB,超过 30MB 视为异常。
if [ "$sz" -gt 31457280 ]; then
  log "错误:bzImage 体积 $((sz/1024/1024))MB 过大,WSL2 可能无法引导,已中止"
  exit 1
fi
cp arch/x86/boot/bzImage "${OUT}.part"
mv "${OUT}.part" "$OUT"
log "完成: $OUT"
