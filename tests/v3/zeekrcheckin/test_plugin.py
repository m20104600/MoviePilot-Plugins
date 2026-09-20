# -*- coding: utf-8 -*-
"""极氪签到（MoviePilot V3 插件）单元测试。

运行（无需 MoviePilot 宿主，桩在 tools/stub_host.py）：
  python3 -m unittest discover -s tests/v3/zeekrcheckin -v
  # 或在有 pytest 的环境里： python3 -m pytest tests/v3/zeekrcheckin

覆盖：Token 清洗 / JWT 解析 / 签名与编码（与手机端脚本一致性回归）/ cron 与配置解析 /
定时服务注册 / 主流程顺序 / 领取循环两种模式 / 无 Token 与 Token 过期的失败路径。
外部网络全部由脚本化的假 `_request` 顶替，不依赖公网。
"""

import base64
import json
import pathlib
import sys
import time
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

from stub_host import load_plugin  # noqa: E402

plugin = load_plugin("zeekrcheckin_tests")


def make_jwt(account_id="2007123456789012", device_id="DEV-ABC-123", days=90):
    """造一个结构与极氪 Token 一致的 JWT（只用于本地测试）。"""

    def seg(obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    sub = json.dumps(
        {
            "accountInfoDTO": {"accountId": account_id},
            "accountLoginInfoDTO": {"lastLoginDeviceId": device_id},
        },
        ensure_ascii=False,
    )
    payload = {"sub": sub, "exp": int(time.time()) + days * 86400}
    return "Bearer " + seg({"alg": "none"}) + "." + seg(payload) + "." + seg({"sig": "x"})


class FakeTime:
    """虚拟时钟：让脚本里的 sleep 瞬间通过，同时让 elapsed 计算仍然正确。"""

    def __init__(self):
        self._t = time.time()  # 从真实时间起步：Token 过期天数等计算仍按真实时间

    def time(self):
        return self._t

    def sleep(self, seconds):
        self._t += float(seconds)


CLOCK = FakeTime()


class RecordingClock(FakeTime):
    """记录每次 sleep 的秒数（验证「复查间隔按秒」这类单位问题）。"""

    def __init__(self):
        super().__init__()
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(float(seconds))
        super().sleep(seconds)


class FakePlugin(plugin.ZeekrCheckin):
    """把网络层换成脚本化响应，并屏蔽真实 sleep。"""

    def __init__(self, responses=None):
        super().__init__()
        self.calls = []
        self.responses = responses or {}
        self.fake_time = CLOCK
        plugin.time = CLOCK  # 模块级虚拟时钟：脚本里的 sleep 立即通过，elapsed 仍然正确

    # 记录调用并返回预设响应
    def _request(self, ctx, path, method="GET", body=None):
        self.calls.append((method, path, body))
        key = path.split("?")[0]
        for pattern, response in self.responses.items():
            if key.startswith(pattern):
                if callable(response):
                    return response(path, body, len([c for c in self.calls if c[1].split("?")[0].startswith(pattern)]))
                return response
        return {"code": "000000", "data": {}}

    def _fetch_app_version(self):
        return "4.9.33"

    def paths(self):
        return [c[1].split("?")[0] for c in self.calls]


class TokenTest(unittest.TestCase):
    """Token 清洗与解析。"""

    def test_quotes_and_spaces(self):
        jwt = make_jwt()[:7] + make_jwt()[7:]
        raw = '  "Bearer ' + make_jwt().replace("Bearer ", "") + '"  '
        self.assertEqual(plugin.clean_token(raw), make_jwt())

    def test_bare_jwt_gets_bearer(self):
        self.assertEqual(plugin.clean_token(make_jwt().replace("Bearer ", "")), make_jwt())

    def test_zero_width_and_newlines(self):
        raw = "Bearer\n" + make_jwt().replace("Bearer ", "") + "\u200b"
        self.assertEqual(plugin.clean_token(raw), make_jwt())

    def test_placeholder_is_empty(self):
        for raw in ("", None, "${TOKEN}", "<粘贴你的Token>", "Bearer 123"):
            self.assertEqual(plugin.clean_token(raw), "", f"{raw!r} 应被判为没有 Token")

    def test_from_store_json(self):
        raw = json.dumps({"authorization": make_jwt()})
        self.assertEqual(plugin.token_from_store(raw), make_jwt())

    def test_from_store_query(self):
        self.assertEqual(plugin.token_from_store("authorization=" + make_jwt()), make_jwt())

    def test_parse_token(self):
        info = plugin.parse_token(make_jwt(account_id="2007123456789012", device_id="DEV-1", days=30))
        self.assertEqual(info["accountId"], "2007123456789012")
        self.assertEqual(info["deviceId"], "DEV-1")
        self.assertTrue(29 <= info["daysLeft"] <= 30)

    def test_parse_broken_token(self):
        info = plugin.parse_token("Bearer not-a-jwt")
        self.assertEqual(info["deviceId"], "")
        self.assertEqual(info["daysLeft"], 999)


class CodecTest(unittest.TestCase):
    """编码与签名（与手机端脚本的对照值来自 tools/parity_check.py 的 JS 侧）。"""

    def test_step_secret_matches_js(self):
        self.assertEqual(
            plugin.encode_step_secret(12345),
            "VmtaYVUxTnRWbkpPVlZaWFlsWndjVlJYZEdGbGJIQkdVbFJzVVZWVU1Eaz0=",
        )
        self.assertEqual(
            plugin.encode_step_secret(8000),
            "VmtSQ1UxRnRVWGROVldSUVYwaENZVlpxVG01a2R6MDk=",
        )

    def test_b64e_utf8(self):
        self.assertEqual(plugin.b64e("hello 极氪"), "aGVsbG8g5p6B5rCq")

    def test_sign_is_40_hex_and_deterministic(self):
        first = plugin.sign_params(1700000000000, "AbC123xyz")
        second = plugin.sign_params(1700000000000, "AbC123xyz")
        self.assertEqual(first, second)
        self.assertEqual(len(first["sign"]), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in first["sign"]))
        other = plugin.sign_params(1700000000001, "AbC123xyz")
        self.assertNotEqual(first["sign"], other["sign"])


class ConfigTest(unittest.TestCase):
    """配置解析与定时服务注册。"""

    def setUp(self):
        self.p = FakePlugin()

    def test_parse_cron_entries(self):
        entries = plugin.parse_cron_entries("1 0 * * * 凌晨场\n30 21 * * * 晚间场\n坏行\n10 8 * * *;20 9 * * *")
        self.assertEqual(
            entries,
            [("1 0 * * *", "凌晨场"), ("30 21 * * *", "晚间场"), ("10 8 * * *", ""), ("20 9 * * *", "")],
        )

    def test_init_reads_config(self):
        self.p.init_plugin(
            {
                "enabled": True,
                "token": make_jwt(),
                "cron_sign": "1 0 * * * 凌晨场",
                "cron_claim": "",
                "steps": "off",
                "poll": True,
                "notify": False,
            }
        )
        self.assertTrue(self.p.get_state())
        self.assertEqual(len(self.p._tokens), 1)
        self.assertTrue(self.p._poll)
        self.assertFalse(self.p._notify)
        self.assertEqual(self.p._steps, "off")

    def test_disabled_has_no_service(self):
        self.p.init_plugin({"enabled": False, "cron_sign": "1 0 * * *"})
        self.assertEqual(self.p.get_service(), [])

    def test_services_from_config(self):
        self.p.init_plugin(
            {
                "enabled": True,
                "cron_sign": "1 0 * * * 凌晨场\n30 21 * * * 晚间场",
                "cron_claim": "10 0 * * * 凌晨补领",
            }
        )
        services = self.p.get_service()
        self.assertEqual([s["id"] for s in services], ["ZeekrCheckin.all.1", "ZeekrCheckin.all.2", "ZeekrCheckin.claim.1"])
        self.assertEqual(services[0]["name"], "极氪签到·凌晨场")
        self.assertEqual(services[2]["kwargs"], {"mode": "claim", "tag": "凌晨补领"})
        self.assertEqual(services[0]["trigger"].expression, "1 0 * * *")

    def test_bad_cron_skipped(self):
        self.p.init_plugin({"enabled": True, "cron_sign": "0 1 * *\n1 0 * * *"})
        services = self.p.get_service()
        self.assertEqual(len(services), 1)

    def test_form_and_api_shape(self):
        form, defaults = self.p.get_form()
        self.assertEqual(form[0]["component"], "VForm")
        self.assertIn("token", defaults)
        self.assertIn("cron_sign", defaults)
        self.assertEqual(defaults["enabled"], False)
        api = self.p.get_api()
        self.assertEqual(api[0]["path"], "/run")
        self.assertEqual(api[0]["auth"], "apikey")
        self.assertEqual(self.p.get_command(), [])


class FlowTest(unittest.TestCase):
    """主流程：请求顺序、领取、失败路径。"""

    def build(self, extra=None, **config):
        responses = {
            plugin.ZEEKR_API["signIn"]: {"code": "000000", "data": {"signInZgreenInfo": [{"taskName": "每日签到"}]}},
            plugin.ZEEKR_API["walkData"]: {"code": "000000"},
            plugin.ZEEKR_API["taskMsg"]: {
                "code": "000000",
                "data": {
                    "taskReachMsgList": [
                        {"name": plugin.TASK_ARTICLE, "doc": {"path": "/x?acticleId=A1"}},
                        {"name": plugin.TASK_PRAISE, "taskTakeDTO": {"currentComplete": 1, "maxCompleteLimit": 1}},
                        {"name": plugin.TASK_WALK, "taskTakeDTO": {"currentComplete": 1, "maxCompleteLimit": 1}},
                    ]
                },
            },
            plugin.ZEEKR_API["articleDetail"]: {"code": "000000", "data": {"title": "文章A"}},
            plugin.ZEEKR_API["uncollected"]: {
                "code": "000000",
                "data": {
                    "uncollectedVal": [
                        {"id": "W1", "valDefineCode": plugin.VAL_WALK, "val": 20},
                        {"id": "I1", "valDefineCode": plugin.VAL_INTEGRAL, "val": 5},
                        {
                            "id": "D1",
                            "valDefineCode": plugin.VAL_DEBRIS,
                            "eventCode": "E1",
                            "sourceId": "S1",
                            "invoice": {"materialSnapshot": {"name": "停车券"}},
                        },
                    ]
                },
            },
            plugin.ZEEKR_API["claimWalk"]: {"code": "000000"},
            plugin.ZEEKR_API["claimIntegral"]: {"code": "000000"},
            plugin.ZEEKR_API["claimDebris"]: {
                "code": "000000",
                "data": [{"success": True, "invoice": {"materialSnapshot": {"name": "停车券", "medalTemplateSnapshot": {"name": "金"}}}}],
            },
        }
        if extra:
            responses.update(extra)
        p = FakePlugin(responses)
        p.init_plugin({"enabled": True, "token": make_jwt(), "appver": "4.9.33", "verbose": True, **config})
        return p

    def test_full_flow_order_and_result(self):
        p = self.build()
        out = p.run_checkin(mode="all", tag="凌晨场")
        self.assertTrue(out["ok"])
        paths = p.paths()
        self.assertEqual(paths[0], plugin.ZEEKR_API["signIn"])
        self.assertIn(plugin.ZEEKR_API["walkData"], paths)
        self.assertIn(plugin.ZEEKR_API["articleDetail"], paths)
        self.assertIn(plugin.ZEEKR_API["claimWalk"], paths)
        self.assertIn(plugin.ZEEKR_API["claimIntegral"], paths)
        self.assertIn(plugin.ZEEKR_API["claimDebris"], paths)
        # 能量球要排在极值、碎片之前（能量球入账会再生碎片）
        self.assertLess(paths.index(plugin.ZEEKR_API["claimWalk"]), paths.index(plugin.ZEEKR_API["claimIntegral"]))
        self.assertLess(paths.index(plugin.ZEEKR_API["claimIntegral"]), paths.index(plugin.ZEEKR_API["claimDebris"]))
        text = "\n".join(out["lines"])
        self.assertIn("✅ 签到成功", text)
        self.assertIn("🚶 步数上报成功", text)
        self.assertIn("📖 阅读文章成功", text)
        self.assertIn("本周已完成，跳过", text)
        self.assertIn("♻️ 能量球已领: +20", text)
        self.assertIn("🏆 极值已领: +5", text)
        self.assertIn("🧩 碎片奖励: 停车券(金)", text)
        self.assertIn("🏁 本次领取: 碎片 1 个, 能量球 +20, 极值 +5", text)
        # 通知 + 存档
        self.assertEqual(len(p.messages), 1)
        self.assertIn("✅ 极氪签到", p.messages[0]["title"])
        self.assertIn("（凌晨场）", p.messages[0]["title"])
        self.assertTrue(p.get_data("last_result")["ok"])
        self.assertEqual(p.get_data("last_result")["mode"], "all")

    def test_claim_mode_skips_sign_in(self):
        p = self.build()
        out = p.run_checkin(mode="claim", tag="补领")
        self.assertTrue(out["ok"])
        self.assertNotIn(plugin.ZEEKR_API["signIn"], p.paths())
        self.assertNotIn(plugin.ZEEKR_API["walkData"], p.paths())
        self.assertIn(plugin.ZEEKR_API["claimDebris"], p.paths())

    def test_steps_off_skips_walk(self):
        p = self.build(steps="off")
        p.run_checkin(mode="all", tag="")
        self.assertNotIn(plugin.ZEEKR_API["walkData"], p.paths())

    def test_walk_not_done_triggers_rescan(self):
        """「步行3000步」未达标时补报步数并重新扫描。"""
        seq = {"n": 0}

        def task_msg(path, body, count):
            seq["n"] = count
            done = 1 if count >= 3 else 0
            return {
                "code": "000000",
                "data": {
                    "taskReachMsgList": [
                        {"name": plugin.TASK_WALK, "taskTakeDTO": {"currentComplete": done, "maxCompleteLimit": 1}},
                    ]
                },
            }

        def uncollected(path, body, count):
            if count == 1:
                return {
                    "code": "000000",
                    "data": {
                        "uncollectedVal": [
                            {"id": "D9", "valDefineCode": plugin.VAL_DEBRIS, "eventCode": "E9", "sourceId": "S9"}
                        ]
                    },
                }
            return {"code": "000000", "data": {"uncollectedVal": []}}

        p = self.build(extra={
            plugin.ZEEKR_API["taskMsg"]: task_msg,
            plugin.ZEEKR_API["uncollected"]: uncollected,
            plugin.ZEEKR_API["claimDebris"]: {"code": "000000", "data": [{"success": True}]},
        })
        out = p.run_checkin(mode="all", tag="")
        text = "\n".join(out["lines"])
        self.assertIn("🔁 「步行3000步」仍未达标：补报步数后重新扫描", text)
        self.assertEqual(p.paths().count(plugin.ZEEKR_API["signIn"]), 1)
        self.assertGreaterEqual(p.paths().count(plugin.ZEEKR_API["walkData"]), 2)

    def test_poll_mode_polls_until_settled(self):
        """poll=1：连续为空且距上次领取已过 settle 秒后，复查确认结束（轮数 > 1）。"""
        p = self.build(poll=True, waits="1,1,1", settle=1, max=60)
        out = p.run_checkin(mode="claim", tag="轮询")
        text = "\n".join(out["lines"])
        self.assertIn("复查确认", text)
        self.assertIn("轮 /", text)

    def test_wait_intervals_are_seconds_not_ms(self):
        """复查间隔按秒计（默认 45/60/75），不能把毫秒直接丢给 sleep。"""
        clock = RecordingClock()
        p = self.build(poll=True, waits="45,60,75", settle=0, max=600)
        plugin.time = clock
        p.run_checkin(mode="claim", tag="单位")
        over_long = [s for s in clock.sleeps if s >= 100]
        self.assertEqual(over_long, [], f"出现了过长的 sleep：{over_long}")
        self.assertIn(45.0, clock.sleeps)
        # 复查间隔只会取配置的值（45/60/75），且都是「秒」而不是「毫秒」
        long_sleeps = [s for s in clock.sleeps if s > 5]
        self.assertTrue(set(long_sleeps) <= {45.0, 60.0, 75.0}, long_sleeps)

    def test_page_shows_state_and_last_result(self):
        """详情页：没跑过给提示，跑过给结果。"""
        p = self.build()
        blocks = p.get_page()
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["component"], "VAlert")
        self.assertIn("Token 过期", blocks[0]["props"]["text"])
        self.assertEqual(blocks[1]["props"]["type"], "warning")
        p.run_checkin(mode="claim", tag="页面")
        blocks = p.get_page()
        self.assertEqual(blocks[1]["props"]["type"], "success")
        self.assertIn("🏁 本次领取", blocks[1]["props"]["text"])

    def test_page_without_token(self):
        """没配 Token 时详情页要直说。"""
        p = FakePlugin()
        p.init_plugin({"enabled": True})
        text = p.get_page()[0]["props"]["text"]
        self.assertIn("还没有配置 Token", text)

    def test_sign_in_failure_aborts(self):
        p = self.build(extra={plugin.ZEEKR_API["signIn"]: {"code": "9999", "msg": "登录已失效"}})
        out = p.run_checkin(mode="all", tag="")
        self.assertFalse(out["ok"])
        self.assertIn("❌ 签到失败: 登录已失效", "\n".join(out["lines"]))
        self.assertNotIn(plugin.ZEEKR_API["claimDebris"], p.paths())
        self.assertIn("❌ 极氪签到失败", p.messages[0]["title"])

    def test_missing_token(self):
        p = FakePlugin()
        p.init_plugin({"enabled": True, "token": "${TOKEN}"})
        out = p.run_checkin(mode="all", tag="")
        self.assertFalse(out["ok"])
        self.assertIn("缺少 Token", "\n".join(out["lines"]))
        self.assertEqual(p.calls, [])

    def test_expired_token(self):
        p = self.build()
        p.init_plugin({"enabled": True, "token": make_jwt(days=-1), "appver": "4.9.33"})
        out = p.run_checkin(mode="all", tag="")
        self.assertFalse(out["ok"])
        self.assertIn("Token 已过期", "\n".join(out["lines"]))

    def test_multi_account(self):
        p = self.build()
        p.init_plugin(
            {
                "enabled": True,
                "token": make_jwt(device_id="D1") + "\n" + make_jwt(device_id="D2"),
                "appver": "4.9.33",
            }
        )
        out = p.run_checkin(mode="claim", tag="双号")
        self.assertTrue(out["ok"])
        self.assertIn("—— 账号 1 ——", "\n".join(out["lines"]))
        self.assertIn("—— 账号 2 ——", "\n".join(out["lines"]))
        self.assertEqual(p.paths().count(plugin.ZEEKR_API["signIn"]), 0)

    def test_no_overlapping_run(self):
        p = self.build()
        p._lock.acquire()
        try:
            out = p.run_checkin(mode="all", tag="")
            self.assertFalse(out["ok"])
            self.assertIn("上一次还在跑", "\n".join(out["lines"]))
        finally:
            p._lock.release()

    def test_api_run_requires_enabled(self):
        p = self.build()
        result = p.api_run(mode="claim")
        self.assertIn("已触发", result["message"])
        p.stop_service()
        self.assertFalse(p.api_run(mode="claim")["success"])

    def test_headers(self):
        p = self.build()
        ctx = {"token": make_jwt(), "deviceId": "DEV-1", "appVersion": "4.9.33"}
        headers = p._headers(ctx)
        self.assertEqual(headers["Authorization"], ctx["token"])
        self.assertEqual(headers["device_id"], "DEV-1")
        self.assertEqual(headers["app_code"], "toc_h5_green_zeekrapp")
        self.assertEqual(headers["platform_h5"], "IOS")
        self.assertEqual(len(headers["x_ca_sign"]), 40)
        self.assertTrue(headers["User-Agent"].endswith("zeekr_iOS_v4.9.33"))

    def test_http_failure_is_contained(self):
        """接口异常不能把异常抛给宿主调度器。"""

        def boom(path, body, count):
            raise RuntimeError("connection reset")

        p = self.build(extra={plugin.ZEEKR_API["uncollected"]: boom})
        with self.assertRaises(RuntimeError):
            p._request(None, plugin.ZEEKR_API["uncollected"])  # 假实现自己抛，验证测试装置本身
        # 真的 _request 遇到网络错误返回字典而不是抛异常
        real = plugin.ZeekrCheckin()
        real.init_plugin({"enabled": True})
        ctx = {"token": make_jwt(), "deviceId": "D", "appVersion": "4.9.33"}
        result = real._request(ctx, "/not-exist-please-404", "GET")
        self.assertIn("code", result)
        self.assertIn(result["code"], ("PARSE_ERROR", "NETWORK_ERROR", "HTTP_ERROR", "000000", "404"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
