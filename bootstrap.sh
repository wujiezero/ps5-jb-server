#!/bin/sh
# Fetch ps5-jb-server and run the installer.
#
#   curl -fsSL https://raw.githubusercontent.com/wujiezero/ps5-jb-server/main/bootstrap.sh | sudo sh
#   curl -fsSL ... | sudo sh -s -- --dry-run
#
# Environment: PS5JB_REF (default main), PS5JB_DIR (default /opt/ps5-jb-server-src)
set -eu
ref=${PS5JB_REF:-main}
dir=${PS5JB_DIR:-/opt/ps5-jb-server-src}
repo=https://github.com/wujiezero/ps5-jb-server

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 运行（sudo sh）" >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    echo "需要 git（apt-get install -y git）" >&2
    exit 1
fi

if [ -d "$dir/.git" ]; then
    echo "更新已有源码 $dir"
    git -C "$dir" fetch --depth 1 origin "$ref"
    git -C "$dir" reset --hard "origin/$ref"
else
    echo "获取源码到 $dir"
    rm -rf "$dir"
    git clone --depth 1 --branch "$ref" "$repo" "$dir"
fi

exec sh "$dir/install.sh" "$@"
