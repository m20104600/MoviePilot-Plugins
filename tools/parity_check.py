#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端口一致性对照（Python 侧）：证明本插件的纯逻辑与手机端脚本逐字节一致。

跑法：
  python3 tools/parity_check.py [--core /path/parts/core.js] [--dist /path/dist/zeekr.js]

对照项：步数 secret 的 5 层 base64、base64 中文、x_ca_sign 的 SHA1 签名。
两边都不打印密钥，只比对派生结果。
"""

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from stub_host import load_plugin  # noqa: E402


def node_side(core: pathlib.Path, dist: pathlib.Path) -> dict:
    """跑 Node 侧的对照脚本。"""
    result = subprocess.run(
        ["node", str(ROOT / "tools" / "parity_core.js"), str(core), str(dist)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SystemExit(f"Node 侧失败：{result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", default="/root/zeekr-ports/parts/core.js")
    parser.add_argument("--dist", default="/root/zeekr-ports/dist/zeekr.js")
    args = parser.parse_args()

    core, dist = pathlib.Path(args.core), pathlib.Path(args.dist)
    if not core.exists() or not dist.exists():
        raise SystemExit("找不到手机端脚本源码：用 --core / --dist 指到 zeekr-ports 里")

    plugin = load_plugin()
    js = node_side(core, dist)

    checks = [
        ("步数 secret（12345 步）", js["step_12345"], plugin.encode_step_secret(12345)),
        ("步数 secret（8000 步）", js["step_8000"], plugin.encode_step_secret(8000)),
        ("base64（中文）", js["b64_hello"], plugin.b64e("hello 极氪")),
        (
            "x_ca_sign（1700000000000/AbC123xyz）",
            js["sign_1700000000000_AbC123xyz"],
            plugin.sign_params(1700000000000, "AbC123xyz")["sign"],
        ),
        (
            "x_ca_sign（1789912534000/kwD3fQz9nR2pXb1）",
            js["sign_1789912534000_kwD3fQz9nR2pXb1"],
            plugin.sign_params(1789912534000, "kwD3fQz9nR2pXb1")["sign"],
        ),
    ]

    failed = 0
    for name, expected, actual in checks:
        ok = expected == actual
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} {name}")
        if not ok:
            print(f"   JS : {expected}")
            print(f"   PY : {actual}")
    print(f"\n{len(checks) - failed}/{len(checks)} 项一致")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
