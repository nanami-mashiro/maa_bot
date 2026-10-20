#!/bin/sh
# 生成 docker-compose.override.yml 内容到 stdout(宿主负责落盘与幂等比对)。
# redroid 镜像与 cmdline 参数的唯一事实源。
# 输入(env):FACT_ARCH=amd64|arm64  FACT_ASHMEM=true|false  [DEPLOY_GPU_MODE]
#           [REPO_DIR=/repo] 仓库在生成容器内的挂载点,用于探测 redroid/libndk_translation.so
# 分辨率/DPI 锁死 1280x720/240:bot 的官服自动登录坐标兜底依赖该分辨率,勿开放覆盖。
set -eu

arch="${FACT_ARCH:?FACT_ARCH is required}"
ashmem="${FACT_ASHMEM:-false}"
gpu_mode="${DEPLOY_GPU_MODE:-guest}"
repo_dir="${REPO_DIR:-/repo}"

if [ "$arch" = "amd64" ]; then
  image="redroid/redroid:11.0.0_ndk"
else
  image="redroid/redroid:11.0.0-latest"
fi

cat <<EOF
# 本文件由 scripts/deploy 按环境探测自动生成,请勿手改(重跑 deploy 会覆盖)。
# 平台: ${arch}, ashmem=${ashmem}, gpu_mode=${gpu_mode}
services:
  redroid:
    image: ${image}
    command:
      - androidboot.redroid_width=1280
      - androidboot.redroid_height=720
      - androidboot.redroid_dpi=240
      - androidboot.redroid_gpu_mode=${gpu_mode}
EOF

if [ "$ashmem" != "true" ]; then
  cat <<EOF
      - androidboot.use_memfd=true
EOF
fi

if [ "$arch" = "amd64" ]; then
  cat <<EOF
      - ro.product.cpu.abilist=x86_64,arm64-v8a,x86,armeabi-v7a,armeabi
      - ro.product.cpu.abilist64=x86_64,arm64-v8a
      - ro.product.cpu.abilist32=x86,armeabi-v7a,armeabi
      - ro.dalvik.vm.isa.arm=x86
      - ro.dalvik.vm.isa.arm64=x86_64
      - ro.enable.native.bridge.exec=1
      - ro.vendor.enable.native.bridge.exec=1
      - ro.vendor.enable.native.bridge.exec64=1
      - ro.dalvik.vm.native.bridge=libndk_translation.so
      - ro.ndk_translation.version=0.2.3
EOF
fi

# 明日方舟 2.7.81+ 在 libndk 0.2.3 下启动约 12s 后 SIGSEGV(SEGV_ACCERR):
# MprotectForGuest 会清掉 guest 页的 PROT_EXEC。用 patch_libndk.py 生成的修补版
# 只读覆盖进容器;文件不存在时不挂载(否则 docker 会建出同名目录)。
if [ "$arch" = "amd64" ] && [ -f "$repo_dir/redroid/libndk_translation.so" ]; then
  cat <<EOF
    volumes:
      - ./redroid/libndk_translation.so:/system/lib64/libndk_translation.so:ro
EOF
fi
