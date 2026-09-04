#!/bin/sh
# 在 maa-bot 镜像内安装并启动明日方舟官服客户端。
# 挂载 /cache(APK 缓存)。env:
#   ADB_TARGET(默认 redroid:5555)
#   APK_URL(默认与 bot client_update 同源的官方下载地址)
#   SKIP_LAUNCH=true 只装不启动
set -eu

TARGET="${ADB_TARGET:-redroid:5555}"
PKG="com.hypergryph.arknights"
APK_URL="${APK_URL:-https://ak.hypergryph.com/downloads/android_lastest}"
APK="/cache/arknights-official.apk"

log() { echo "[game-setup] $*"; }

adb connect "$TARGET" >/dev/null 2>&1 || true

if adb -s "$TARGET" shell pm list packages "$PKG" 2>/dev/null | grep -q "$PKG"; then
  log "已安装 $PKG,跳过安装"
else
  if [ ! -s "$APK" ]; then
    log "下载官服 APK(约 2GB,断点续传;官网 CDN 直连,绕过代理)"
    curl -fL -C - --retry 3 --noproxy '*' -o "${APK}.part" "$APK_URL"
    mv "${APK}.part" "$APK"
  else
    log "命中 APK 缓存: $APK ($(du -m "$APK" | cut -f1)MB)"
  fi

  log "安装 APK(adb install -r,大文件需数分钟)"
  if ! adb -s "$TARGET" install -r "$APK"; then
    log "install 失败,兜底走 push + pm install"
    adb -s "$TARGET" push "$APK" /data/local/tmp/arknights.apk
    adb -s "$TARGET" shell pm install -r /data/local/tmp/arknights.apk
    adb -s "$TARGET" shell rm -f /data/local/tmp/arknights.apk
  fi
  log "安装完成"
fi

if [ "${SKIP_LAUNCH:-}" = "true" ]; then
  exit 0
fi

# 已有存活进程直接判定通过:重复 am start 会触发 activity 重启,
# 与 20s 检查点竞态出现假阴性(2026-09-03 踩出)。
pid0="$(adb -s "$TARGET" shell pidof "$PKG" 2>/dev/null | tr -d '\r\n ' || true)"
if [ -n "$pid0" ]; then
  log "游戏进程已在运行 (pid=$pid0),跳过拉起"
  exit 0
fi
log "启动游戏进程验证(am start 拉起 launcher activity)"
# redroid 无物理键,monkey 常以 -5 退出(SYS_KEYS 抱怨),am start 才可靠;
# monkey 仅留作 resolve 失败时的兜底。(2026-09-03 踩出)
ACT="$(adb -s "$TARGET" shell cmd package resolve-activity --brief -c android.intent.category.LAUNCHER "$PKG" 2>/dev/null | tail -1 | tr -d '\r')"
case "$ACT" in
  "$PKG"/*) adb -s "$TARGET" shell am start -n "$ACT" >/dev/null 2>&1 || true ;;
  *) adb -s "$TARGET" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true ;;
esac
sleep 20
pid="$(adb -s "$TARGET" shell pidof "$PKG" 2>/dev/null | tr -d '\r\n ' || true)"
if [ -n "$pid" ]; then
  log "游戏进程已运行 (pid=$pid)。首次启动伴随资源解包/转译,黑屏数分钟属正常。"
else
  log "错误:游戏进程未存活,可能转译不可用或安装损坏。logcat 尾部:"
  adb -s "$TARGET" shell logcat -d -t 50 2>/dev/null | tail -30 || true
  exit 23
fi
