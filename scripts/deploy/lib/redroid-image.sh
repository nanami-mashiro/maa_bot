#!/bin/sh
# 在 docker:cli 容器内(挂 /var/run/docker.sock 与 /cache)构建
# redroid 11.0.0 + libndk ARM 转译镜像: redroid/redroid:11.0.0_ndk
# 仅 amd64 需要(官服 APK 只有 ARM 库)。libndk 上游仅在 redroid 11.0.0 验证可用。
# 环境变量: HTTP_PROXY/HTTPS_PROXY 可选(GitHub 下载 libndk 预编译包用)。
set -eu

IMAGE="redroid/redroid:11.0.0_ndk"
# ayasa520/redroid-script 固定 commit,避免上游漂移。
SCRIPT_REPO="https://github.com/ayasa520/redroid-script.git"
SCRIPT_COMMIT="${REDROID_SCRIPT_COMMIT:-master}"

log() { echo "[redroid-image] $*"; }

if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "镜像已存在,跳过: $IMAGE"
  exit 0
fi

apk add --no-cache -q python3 py3-requests py3-tqdm git bash lzip

# 基础镜像 pre-pull:守护进程侧网络,大层可能停滞;docker pull 支持断点续传,
# 带超时重试比单次长等更有效。
try_pull() {
  n=0
  while [ $n -lt 3 ]; do
    n=$((n+1))
    log "拉取 redroid/redroid:11.0.0-latest(第 ${n}/3 次,单次限时 600s)"
    if timeout 600 docker pull redroid/redroid:11.0.0-latest; then
      return 0
    fi
    log "拉取超时/失败,重试(已下载层会续传)"
  done
  return 1
}
if ! docker image inspect redroid/redroid:11.0.0-latest >/dev/null 2>&1; then
  if ! try_pull; then
    log "错误:基础镜像拉取多次停滞。请为 Docker 守护进程配置代理或 registry mirror 后重跑。"
    exit 20
  fi
fi

WORKDIR=/cache/redroid-script
if [ -d "$WORKDIR/.git" ]; then
  log "复用 redroid-script 缓存,校正到 ${SCRIPT_COMMIT}"
  git -C "$WORKDIR" fetch -q origin "$SCRIPT_COMMIT" 2>/dev/null || true
  git -C "$WORKDIR" checkout -q "$SCRIPT_COMMIT" 2>/dev/null || true
else
  log "克隆 redroid-script"
  if [ -n "${HTTPS_PROXY:-}" ]; then
    git -c "http.proxy=${HTTPS_PROXY}" clone -q "$SCRIPT_REPO" "$WORKDIR" \
      || git clone -q "$SCRIPT_REPO" "$WORKDIR"
  else
    git clone -q "$SCRIPT_REPO" "$WORKDIR"
  fi
  git -C "$WORKDIR" checkout -q "$SCRIPT_COMMIT" 2>/dev/null || true
fi

# 脚本在挂载盘上运行会有 exec 位/大小写问题,拷到容器文件系统跑。
cp -r "$WORKDIR" /work
cd /work
export USER=root
log "运行 redroid-script: python3 redroid.py -a 11.0.0 -n"
python3 redroid.py -a 11.0.0 -n

docker image inspect "$IMAGE" >/dev/null 2>&1 || { log "错误:构建后未找到 $IMAGE"; exit 22; }
log "完成: $IMAGE"
