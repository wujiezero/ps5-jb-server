#!/bin/sh
# Regenerates the RAR5 fixtures used by tests/test_archive.py.
# Needs RarLab rar (Debian non-free: apt install rar) and a UTF-8 locale.
set -e
export LANG=C.UTF-8 LC_ALL=C.UTF-8
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
game="$work/PPSA03671-app"
mkdir -p "$game/sce_sys/trophy2" "$game/data/子目录" "$game/empty"
printf '{"titleId":"PPSA03671","localizedParameters":{"defaultLanguage":"en-US","en-US":{"titleName":"Fixture Game"}}}' \
  > "$game/sce_sys/param.json"
# Deterministic pseudo-random content so the fixtures are reproducible.
python3 -c "import random,sys; r=random.Random(7); sys.stdout.buffer.write(bytes(r.getrandbits(8) for _ in range(24000)))" \
  > "$game/eboot.bin"
python3 -c "import sys; sys.stdout.write('texture block\n' * 3000)" > "$game/data/子目录/压缩 文件.dat"
: > "$game/data/zero.bin"
cd "$work"
rm -f "$here"/*.rar
rar a -idq -r -m0 -v10k -tl "$here/store.rar" PPSA03671-app
rar a -idq -r -m3 -v10k "$here/m3.rar" PPSA03671-app
rar a -idq -r -m3 "$here/single.rar" PPSA03671-app
# Only the first volume is needed: these are refused before extraction starts.
for kind in solid enc henc; do
  case $kind in solid) opt=-s ;; enc) opt=-psecret ;; henc) opt=-hpsecret ;; esac
  rar a -idq -r -m3 $opt -v10k "$work/$kind.rar" PPSA03671-app
  cp "$work/$kind.part1.rar" "$here/"
done
ls -l "$here"
