#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真接口冒烟：用你自己的 Token 真跑一次插件逻辑（不依赖 MoviePilot 宿主）。

用法：
  python3 tools/smoke_live.py                  # 默认 MODE=claim（只领取，幂等安全）
  python3 tools/smoke_live.py --mode sign      # 只签到 + 做任务
  python3 tools/smoke_live.py --mode all       # 全流程（签到+步数+任务+领取）
  ZEEKR_TOKEN="Bearer eyJ..." python3 tools/smoke_live.py

Token 来源：环境变量 ZEEKR_TOKEN → /root/.config/zeekr-checkin/env（服务器版 systemd 用的那份）。
通知不会真的发出去，只打印到终端；任何时候都不打印 Token 本身。
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from stub_host import load_plugin  # noqa: E402

ENV_FILE = pathlib.Path("/root/.config/zeekr-checkin/env")


def token_from_env_file() -> str:
    """从 systemd 版的 env 文件里读 ZEEKR_TOKEN（不打印内容）。"""
    if not ENV_FILE.exists():
        return ""
    text = ENV_FILE.read_text(encoding="utf-8", errors="replace")
    for key in ("ZEEKR_TOKEN", "ZEEKR_VAL", "zeekr_val"):
        match = re.search(rf"^{key}\s*=\s*(.+)$", text, re.MULTILINE)
        if match:
            return match.group(1).strip().strip("'\"")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="claim", choices=["all", "sign", "claim"])
    parser.add_argument("--tag", default="冒烟")
    parser.add_argument("--steps", default="10000")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="走「立即运行一次」开关那条路（init_plugin(run_now=True) + 后台线程 + 自动复位开关）",
    )
    args = parser.parse_args()

    import os

    token = os.environ.get("ZEEKR_TOKEN", "").strip() or token_from_env_file()
    if not token:
        print("❌ 没有 Token：设 ZEEKR_TOKEN，或让 /root/.config/zeekr-checkin/env 里带 ZEEKR_TOKEN")
        return 2

    plugin = load_plugin()
    inst = plugin.ZeekrCheckin()
    mode = "all" if args.run_now else args.mode
    config = {
        "enabled": True,
        "token": token,
        "steps": args.steps,
        "poll": args.poll,
        "verbose": args.verbose,
        "notify": True,
    }
    if args.run_now:
        # 走「立即运行一次」那条路：保存配置 → 后台线程跑 → 开关自动复位
        config["run_now"] = True
        print("走「立即运行一次」开关（等价于配置页打开开关并保存）")
        inst.init_plugin(config)
        if inst._thread is None:
            print("⚠️ 开关没有触发（上一次还在跑？）")
            return 1
        inst._thread.join()
        print(f"开关复位后的配置：run_now={inst.get_config().get('run_now')}")
    else:
        inst.init_plugin(config)

    info = plugin.parse_token(inst._tokens[0])
    print(f"账号 {info['accountId']}｜设备 {info['deviceId'][:6]}***｜Token 剩 {info['daysLeft']} 天")
    print(f"—— 开始 {mode} ——")
    result = inst.run_checkin(mode=mode, tag=args.tag) if not args.run_now else inst.get_data("last_result") or {}
    print("—— 通知（本应发到 MP 通知渠道）——")
    for message in inst.messages:
        print(f"【{message['title']}】\n{message['text']}")
    print(f"—— 结束，ok={bool(result.get('ok'))} ——")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
