#!/usr/bin/env python3
"""Compile v2fly domain-list-community files into one JSON used for classification.

Upstream: https://github.com/v2fly/domain-list-community (MIT).
We only support literal/domain/full/suffix entries; regexp/include are skipped.
"""
import json
import pathlib

RAW = pathlib.Path(__file__).resolve().parents[1] / "rulesets" / "raw"
OUT = pathlib.Path(__file__).resolve().parents[1] / "rulesets" / "compiled.json"
# Order matters: first match wins, so the most specific categories come first.
CATEGORIES = [
    ("playstation.txt", "PlayStation 服务", "suffix"),
    ("sony.txt", "Sony 服务", "suffix"),
    ("category-games.txt", "游戏平台", "suffix"),
    ("category-media.txt", "流媒体 / 媒体", "suffix"),
    ("category-ads-all.txt", "广告与追踪", "suffix"),
]


def entries(path):
    suffix, exact = set(), set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("regexp:", "include:", "keyword:")):
            continue
        if line.startswith("full:"):
            exact.add(line[5:].strip().lower().rstrip("."))
        elif line.startswith(("domain:", "suffix:")):
            suffix.add(line.split(":", 1)[1].strip().lower().rstrip("."))
        elif ":" not in line:
            suffix.add(line.lower().rstrip("."))
    return sorted(x for x in suffix if x), sorted(x for x in exact if x)


def main():
    compiled = {"source": "v2fly/domain-list-community (MIT)", "compiled": {}, "exact": {}}
    for filename, label, _ in CATEGORIES:
        path = RAW / filename
        if not path.exists():
            print("missing", filename)
            continue
        suffix, exact = entries(path)
        compiled["compiled"][label] = suffix
        if exact:
            compiled["exact"][label] = exact
        print(f"{label:14s} suffix={len(suffix):4d} exact={len(exact):3d}")
    # Heuristic CDN buckets: these are suffixes, not an upstream list.
    compiled["compiled"]["CDN（启发式）"] = sorted({
        "akamai.net", "akamaiedge.net", "akamaihd.net", "edgekey.net", "edgesuite.net",
        "cloudflare.com", "cloudfront.net", "fastly.net", "fastlylb.net",
        "amazonaws.com", "googleusercontent.com", "gstatic.com", "googleapis.com",
        "azureedge.net", "azure.com", "trafficmanager.net", "llnwd.net", "cdn77.org",
    })
    OUT.write_text(json.dumps(compiled, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(len(v) for v in compiled["compiled"].values())
    print(f"\nwrote {OUT} ({OUT.stat().st_size} bytes, {total} suffixes)")


if __name__ == "__main__":
    main()
