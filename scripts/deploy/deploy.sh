#!/bin/bash
# MAA Telegram Bot 一键部署 —— 原生 Linux 入口(amd64/arm64)。
# Windows/WSL2 请在 Windows PowerShell 运行 scripts/deploy/deploy.ps1
# (内核切换失败后唯一可用的恢复环境在 Windows 宿主,恢复逻辑必须住在 ps1 里)。
#
# 用法: bash scripts/deploy/deploy.sh [--yes] [--no-launch] [--persist-modules]
set -euo pipefail

YES=false; NO_LAUNCH=false; PERSIST_MODULES=false
for a in "$@"; do
  case "$a" in
    --yes) YES=true ;;
    --no-launch) NO_LAUNCH=true ;;
    --persist-modules) PERSIST_MODULES=true ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CACHE_DIR="$REPO_ROOT/.deploy-cache"
mkdir -p "$CACHE_DIR/logs"
exec > >(tee -a "$CACHE_DIR/logs/deploy-$(date +%Y%m%d-%H%M%S).log") 2>&1

log() { echo "[deploy] $*"; }
fail() { code=$1; shift; echo "[deploy] 错误: $*" >&2; exit "$code"; }

# ---------- P0 preflight ----------
case "$(uname -r)" in
  *microsoft-standard-WSL2*) fail 2 '检测到 WSL 环境:请在 Windows PowerShell 运行 scripts/deploy/deploy.ps1' ;;
esac
case "$(uname -s)" in
  MINGW*|MSYS*) fail 2 '检测到 Git Bash:请在 Windows PowerShell 运行 scripts/deploy/deploy.ps1' ;;
esac
command -v docker >/dev/null || fail 2 '未安装 docker'
docker info >/dev/null 2>&1 || fail 2 'Docker 引擎未运行'
docker compose version >/dev/null 2>&1 || fail 2 '需要 docker compose v2'

[ -f "$REPO_ROOT/.env" ] || fail 40 '.env 不存在(cp .env.example .env 并填写)'
set -a; . "$REPO_ROOT/.env"; set +a
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] || fail 40 '.env 缺少 TELEGRAM_BOT_TOKEN'
[ -n "${TELEGRAM_ALLOWED_USER_IDS:-}" ] || fail 40 '.env 缺少 TELEGRAM_ALLOWED_USER_IDS'
PROXY="${HTTPS_PROXY:-${HTTP_PROXY:-${ALL_PROXY:-}}}"
# 原生 Linux 上没有 192.168.65.254(那是 Docker Desktop 网关);容器内应使用
# host.docker.internal(compose 需 extra_hosts: host-gateway)或宿主局域网 IP。
case "$PROXY" in
  *192.168.65.254*) log '警告:.env 代理指向 Docker Desktop 网关,原生 Linux 上不可达,请改为宿主可达地址' ;;
esac

# ---------- P1 probe ----------
log '探测环境(容器内)...'
probe_args=(-v "$CACHE_DIR:/cache")
[ -n "$PROXY" ] && probe_args+=(-e "PROBE_PROXY=$PROXY")
PROBE_OUT="$(docker run --rm "${probe_args[@]}" -v "$REPO_ROOT:/repo:ro" alpine \
  sh -c "tr -d '\r' </repo/scripts/deploy/lib/probe.sh >/tmp/s && sh /tmp/s")" || fail 20 '探针执行失败'
eval "$(echo "$PROBE_OUT" | sed 's/^/FACT_/')"
log "探测结果: arch=$FACT_ARCH kernel=$FACT_KERNEL binder=$FACT_BINDER_OK ashmem=$FACT_ASHMEM proxy_ok=$FACT_PROXY_OK"

# ---------- P2 binder(原生 Linux) ----------
if [ "$FACT_BINDER_OK" != "true" ]; then
  if docker info --format '{{.OperatingSystem}}' | grep -qi 'docker desktop'; then
    fail 30 'Docker Desktop for Linux 的引擎跑在 VM 里,宿主 modprobe 无效;请改用发行版原生 docker 引擎或参考 README'
  fi
  log 'binder 缺失,尝试 modprobe binder_linux...'
  if sudo modprobe binder_linux devices=binder,hwbinder,vndbinder 2>/dev/null; then
    PROBE_OUT="$(docker run --rm -v "$REPO_ROOT:/repo:ro" alpine \
      sh -c "tr -d '\r' </repo/scripts/deploy/lib/probe.sh >/tmp/s && sh /tmp/s")"
    eval "$(echo "$PROBE_OUT" | sed 's/^/FACT_/')"
  fi
  if [ "$FACT_BINDER_OK" != "true" ]; then
    fail 30 '内核不支持 binder 且 binder_linux 模块不可用。请安装带 binder 的内核(如 linux-zen)或自编内核开启 CONFIG_ANDROID_BINDER_IPC/BINDERFS'
  fi
  log 'binder 已通过 binder_linux 模块启用'
  if $PERSIST_MODULES; then
    echo binder_linux | sudo tee /etc/modules-load.d/binder.conf >/dev/null
    echo 'options binder_linux devices=binder,hwbinder,vndbinder' | sudo tee /etc/modprobe.d/binder.conf >/dev/null
    log '已写入 /etc/modules-load.d 与 /etc/modprobe.d 持久化'
  else
    log '提示:重启后失效;用 --persist-modules 或手动写 /etc/modules-load.d/binder.conf 持久化'
  fi
fi

# ---------- P3 redroid 镜像 ----------
if [ "$FACT_ARCH" = "amd64" ]; then
  if ! docker image inspect redroid/redroid:11.0.0_ndk >/dev/null 2>&1; then
    log '构建 redroid 11.0.0 + libndk 转译镜像...'
    nargs=(-v /var/run/docker.sock:/var/run/docker.sock -v "$CACHE_DIR:/cache")
    [ -n "$PROXY" ] && nargs+=(-e "HTTPS_PROXY=$PROXY" -e "HTTP_PROXY=$PROXY")
    docker run --rm "${nargs[@]}" -v "$REPO_ROOT:/repo:ro" docker:cli \
      sh -c "tr -d '\r' </repo/scripts/deploy/lib/redroid-image.sh >/tmp/s && sh /tmp/s" \
      || fail 22 'redroid ndk 镜像构建失败(拉取停滞可为 dockerd 配 daemon.json proxies 后重跑)'
  else
    log 'redroid ndk 镜像已存在,跳过'
  fi
else
  docker image inspect redroid/redroid:11.0.0-latest >/dev/null 2>&1 \
    || docker pull redroid/redroid:11.0.0-latest || fail 20 'redroid 镜像拉取失败'
fi

# ---------- P4 override ----------
log '生成 docker-compose.override.yml...'
OVERRIDE="$(docker run --rm -e "FACT_ARCH=$FACT_ARCH" -e "FACT_ASHMEM=$FACT_ASHMEM" \
  -v "$REPO_ROOT:/repo:ro" alpine \
  sh -c "tr -d '\r' </repo/scripts/deploy/lib/gen-override.sh >/tmp/s && sh /tmp/s")" || fail 22 'override 生成失败'
if [ ! -f "$REPO_ROOT/docker-compose.override.yml" ] \
  || [ "$(cat "$REPO_ROOT/docker-compose.override.yml")" != "$OVERRIDE" ]; then
  printf '%s\n' "$OVERRIDE" > "$REPO_ROOT/docker-compose.override.yml"
  log 'override 已更新'
fi
cd "$REPO_ROOT"
docker compose config -q || fail 40 'compose 配置校验失败'

# ---------- P5-P9 ----------
# busybox wget 走 https 代理不可靠,用 curl 镜像单独实测代理连通性。
PROXY_OK=false
if [ -n "$PROXY" ] && docker run --rm curlimages/curl -sS -m 12 -x "$PROXY" -o /dev/null https://api.telegram.org/ >/dev/null 2>&1; then
  PROXY_OK=true
fi
if [ "$PROXY_OK" = "true" ]; then export HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"; fi
log '构建 maa-bot 镜像...'
docker compose build maa-bot || fail 22 'maa-bot 构建失败'

log '启动 redroid...'
docker compose up -d redroid
LIBV=(-v "$REPO_ROOT/scripts/deploy/lib:/deploylib:ro" -v "$CACHE_DIR:/cache")
docker compose run --rm --no-deps "${LIBV[@]}" maa-bot \
  sh -c "tr -d '\r' </deploylib/android-ready.sh >/tmp/s && sh /tmp/s" \
  || { docker logs redroid --tail 60 || true; fail 21 'redroid 启动超时'; }

GENV=""
[ -n "${CLIENT_UPDATE_DOWNLOAD_URL:-}" ] && GENV="APK_URL='$CLIENT_UPDATE_DOWNLOAD_URL' "
$NO_LAUNCH && GENV="${GENV}SKIP_LAUNCH=true "
docker compose run --rm --no-deps "${LIBV[@]}" maa-bot \
  sh -c "tr -d '\r' </deploylib/game-setup.sh >/tmp/s && ${GENV}sh /tmp/s" || fail 23 '游戏安装/启动失败'

docker compose run --rm --no-deps "${LIBV[@]}" maa-bot \
  sh -c "tr -d '\r' </deploylib/maacore-install.sh >/tmp/s && sh /tmp/s" || fail 22 'MaaCore 安装失败'

log '启动 maa-bot...'
docker compose up -d
echo '================ 部署摘要 ================'
docker compose run --rm --no-deps "${LIBV[@]}" maa-bot \
  sh -c "tr -d '\r' </deploylib/verify.sh >/tmp/s && sh /tmp/s" || true
echo '=========================================='
echo '首次登录如遇验证码:scrcpy 连接宿主 127.0.0.1:5555 手动完成一次;登录态存 redroid-data 卷。'
