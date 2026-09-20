#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把极氪签名 key 填进插件源码（占位符 __ZEEKR_SECRET__ → 真实值）。

密钥是极氪 H5 前端的固定签名 key（公开前端里就有，不是账号凭证），
仓库里以占位符形式存在，构建/安装后由本脚本填入，避免明文散落在源码里。

密钥来源按优先级：
  1) 环境变量 ZEEKR_SECRET_SRC 指向的文件（从中匹配 export const SECRET = "..." / var ZEEKR_SECRET = "..."）
  2) 已发布的 zeekr-checkin 仓库脚本 dist/zeekr.js（默认从 GitHub raw 取，可离线：--from 本地文件）

用法：
  python3 tools/inject_secret.py                 # 从 GitHub raw 取密钥并填入
  python3 tools/inject_secret.py --from xx.js    # 用本地文件（离线）
  python3 tools/inject_secret.py --check         # 只检查占位符是否已填，不改文件
"""

import argparse
import os
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "plugins.v3" / "zeekrcheckin" / "__init__.py"
PLACEHOLDER = "__ZEEKR_SECRET__"
REMOTE_SOURCES = [
    "https://raw.githubusercontent.com/m20104600/zeekr-checkin/main/dist/zeekr.js",
    "https://cdn.jsdelivr.net/gh/m20104600/zeekr-checkin@main/dist/zeekr.js",
]
LOCAL_SOURCES = [
    pathlib.Path("/root/zeekr-ports/dist/zeekr.js"),
    pathlib.Path.home() / ".hermes/skills/zeekr-auto-checkin/scripts/checkin.mjs",
]
PATTERNS = [
    re.compile(r'var ZEEKR_SECRET = "([^"]+)"'),
    re.compile(r'export const SECRET\s*=\s*\n?\s*"([^"]+)"'),
]


def find_secret(text: str):
    """从给定文本里提取密钥。"""
    for pattern in PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def load_secret(from_file: str = "") -> str:
    """按优先级拿密钥；只返回密钥本身，不打印。"""
    env_src = os.environ.get("ZEEKR_SECRET_SRC", "").strip()
    files = []
    if env_src:
        files.append(pathlib.Path(env_src))
    if from_file:
        files.append(pathlib.Path(from_file))
    files.extend(LOCAL_SOURCES)
    for path in files:
        try:
            if path.exists():
                secret = find_secret(path.read_text(encoding="utf-8"))
                if secret:
                    print(f"密钥来源: {path}")
                    return secret
        except Exception as error:
            print(f"跳过 {path}：{error}")
    for url in REMOTE_SOURCES:
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                secret = find_secret(resp.read().decode("utf-8", "replace"))
            if secret:
                print(f"密钥来源: {url}")
                return secret
        except Exception as error:
            print(f"跳过 {url}：{error}")
    raise SystemExit("❌ 没找到签名密钥：用 --from 指定一份 dist/zeekr.js，或设环境变量 ZEEKR_SECRET_SRC")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_file", default="", help="本地密钥来源文件")
    parser.add_argument("--check", action="store_true", help="只检查是否已填")
    args = parser.parse_args()

    text = TARGET.read_text(encoding="utf-8")
    filled = PLACEHOLDER not in text
    if args.check:
        print("✅ 已填入密钥" if filled else "⚠️ 仍是占位符 __ZEEKR_SECRET__")
        return 0 if filled else 1

    secret = load_secret(args.from_file)
    # 用 replace 而不是正则，避免密钥里的特殊字符被当成正则
    if PLACEHOLDER in text:
        TARGET.write_text(text.replace(PLACEHOLDER, secret), encoding="utf-8")
        print(f"✅ 已写入 {TARGET.relative_to(ROOT)}（密钥 {len(secret)} 字符）")
    else:
        # 已填过：按来源值刷新（密钥轮换时用得上），但仍不打印原文
        updated = re.sub(r'ZEEKR_SECRET = "[^"]*"', f'ZEEKR_SECRET = "{secret}"', text)
        if updated != text:
            TARGET.write_text(updated, encoding="utf-8")
            print(f"✅ 已更新 {TARGET.relative_to(ROOT)} 里的密钥（{len(secret)} 字符）")
        else:
            print("✅ 密钥已是最新，无需改动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
