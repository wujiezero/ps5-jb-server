#!/bin/sh
# Fetch ps5-jb-server and run the installer.
#
#   curl -fsSL https://raw.githubusercontent.com/wujiezero/ps5-jb-server/main/bootstrap.sh | sudo sh
#   curl -fsSL ... | sudo sh -s -- --dry-run
#
# git, python3 and CA certificates are installed automatically when missing
# (apt/dnf/yum/apk/pacman/zypper); pass --no-deps to only report them.
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

no_deps=0
for arg in "$@"; do
    [ "$arg" = "--no-deps" ] && no_deps=1
done

pkg_install() {
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y --no-install-recommends "$@" \
            || { apt-get update && apt-get install -y --no-install-recommends "$@"; }
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y "$@"
    elif command -v yum >/dev/null 2>&1; then
        yum install -y "$@"
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache "$@"
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm --needed $(printf '%s\n' "$@" | sed 's/^python3$/python/')
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install "$@"
    else
        echo "没有识别到包管理器（apt/dnf/yum/apk/pacman/zypper），请手动安装：$*" >&2
        return 1
    fi
}

# git clones the source; python3 runs the installer; CA certificates let both
# reach GitHub over HTTPS on a minimal container image.
need=""
command -v git >/dev/null 2>&1 || need="$need git"
command -v python3 >/dev/null 2>&1 || need="$need python3"
if [ ! -s /etc/ssl/certs/ca-certificates.crt ] && [ ! -s /etc/pki/tls/certs/ca-bundle.crt ] \
        && [ ! -s /etc/ssl/ca-bundle.pem ] && [ ! -s /etc/ssl/cert.pem ]; then
    need="$need ca-certificates"
fi
if [ -n "$need" ]; then
    if [ "$no_deps" -eq 1 ]; then
        echo "缺少依赖：$need（去掉 --no-deps 可自动安装）" >&2
        exit 1
    fi
    echo "自动安装缺少的依赖：$need"
    # shellcheck disable=SC2086 # word splitting is the point
    pkg_install $need || { echo "依赖安装失败，请手动安装：$need" >&2; exit 1; }
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
