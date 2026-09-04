#!/bin/sh
# 在 maa-bot 镜像内(compose run,接入项目网络)等待 redroid 启动完成。
# env: ADB_TARGET(默认 redroid:5555)、BOOT_TIMEOUT(默认 600 秒;
#      libndk 首次启动明显偏慢,超时不宜设短)。
set -eu

TARGET="${ADB_TARGET:-redroid:5555}"
TIMEOUT="${BOOT_TIMEOUT:-600}"

log() { echo "[android-ready] $*"; }

log "等待 ADB 连接 ${TARGET}(限时 ${TIMEOUT}s;首次启动含系统初始化,较慢属正常)"
start=$(date +%s)
while :; do
  adb connect "$TARGET" >/dev/null 2>&1 || true
  state="$(adb -s "$TARGET" get-state 2>/dev/null || true)"
  if [ "$state" = "device" ]; then
    boot="$(adb -s "$TARGET" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r\n ' || true)"
    if [ "$boot" = "1" ]; then
      log "系统启动完成"
      adb -s "$TARGET" shell getprop ro.product.cpu.abilist | tr -d '\r' | sed 's/^/[android-ready] abilist: /'
      exit 0
    fi
  fi
  now=$(date +%s)
  if [ $((now - start)) -ge "$TIMEOUT" ]; then
    log "错误:等待 boot_completed 超时(${TIMEOUT}s)。排查:docker logs redroid;"
    log "常见原因:内核无 binder、use_memfd 参数、gpu_mode、误用非转译镜像;"
    log "脏数据卷可重置:docker compose down redroid && docker volume rm <项目前缀>_redroid-data"
    exit 21
  fi
  sleep 5
done
