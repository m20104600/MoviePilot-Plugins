#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验发布元数据一致性（对应 MP 官方仓库的 check_plugin_versions 门禁）。

三处必须一致：插件类的 plugin_version / package.v3.json 的 version / package.v3.json 里 history 顶部版本。
用法： python3 tools/check_versions.py
"""

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "package.v3.json"


def class_versions() -> dict:
    """扫描 plugins.v3/*/__init__.py 里的插件类名与 plugin_version。"""
    out = {}
    for init in (ROOT / "plugins.v3").glob("*/__init__.py"):
        text = init.read_text(encoding="utf-8")
        cls = re.search(r"^class\s+(\w+)\s*\(_PluginBase\)", text, re.MULTILINE)
        version = re.search(r'^\s*plugin_version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if cls and version:
            out[init.parent.name] = (cls.group(1), version.group(1))
    return out


def main() -> int:
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    failed = 0
    for plugin_dir, (plugin_id, version) in sorted(class_versions().items()):
        if plugin_dir != plugin_id.lower():
            print(f"❌ 目录名 {plugin_dir} 与类名 {plugin_id} 的小写形式不一致")
            failed += 1
        entry = index.get(plugin_id)
        if not entry:
            print(f"❌ {plugin_dir}: package.v3.json 里没有 {plugin_id} 条目")
            failed += 1
            continue
        history = entry.get("history") or {}
        newest = next(iter(history), None)
        if entry.get("version") != version:
            print(f"❌ {plugin_id}: 类里 {version} != 索引里 {entry.get('version')}")
            failed += 1
        if newest != f"v{version}":
            print(f"❌ {plugin_id}: history 顶部是 {newest}，应为 v{version}")
            failed += 1
        if not failed:
            print(f"✅ {plugin_id}: {version}（类 / 索引 / history 一致）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
