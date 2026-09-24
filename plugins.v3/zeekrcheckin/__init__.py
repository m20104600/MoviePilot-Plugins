"""极氪 App 自动签到 · MoviePilot V3 插件。

一套逻辑移植自 zeekr-checkin 的多客户端脚本（QX / Loon / Stash / Egern / 青龙），
行为等价：签到 → 上报步数 → 做任务（阅读文章 / 每周点赞）→ 领取（碎片 / 能量球 / 极值），
含「奖励延迟入账」的补领设计。

本插件与手机端脚本的**唯一区别**：不包含 Token 抓取。
Token 由用户在手机上抓一次（Egern / QX / Loon / Stash 的抓取规则，或自己抓包），
复制 `Bearer eyJ...` 粘进插件设置即可；插件只读配置里的 Token，不发任何登录/换票请求。
"""

import base64
import hashlib
import json
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.logging import logger

# 插件图标（放在本仓库 icons/ 目录；仓库改名时同步这份和 package.v3.json）
ICON_URL = (
    "https://raw.githubusercontent.com/m20104600/MoviePilot-Plugins/main/icons/zeekr_checkin.png"
)

# 极氪 H5 前端的固定签名 key（公开前端里就有，不是账号凭证）；构建时由 tools/inject_secret.py 填入
ZEEKR_SECRET = "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCz09z6e9WOcNq+nUMX8Vq1Xe2EmJxuR3XbturefioF)E(Fl"
ZEEKR_BASE = "https://api-gw-toc.zeekrlife.com"
ITUNES_LOOKUP = "https://itunes.apple.com/cn/lookup?id=1570277888"
DEFAULT_APP_VERSION = "4.9.33"

ZEEKR_API = {
    "signIn": "/zeekrlife-mp-val/toc/v1/zgreen/center",
    "taskMsg": "/zeekrlife-mp-mkt/open/v1/taskProgress/taskMsg",
    "walkData": "/zeekrlife-mp-val/v1/walkData/initDayWalkData",
    "articleDetail": "/zeekrlife-bbs-theme/v1/invitation/pub/detail",
    "squareList": "/zeekrlife-bbs-theme/v1/invitation/pub/list",
    "fabulous": "/zeekrlife-bbs-theme/v1/clicks/fabulous",
    "uncollected": "/zeekrlife-mp-val/v1/carEnergy/getUncollectedBallsPageNew",
    "claimDebris": "/zeekrlife-mp-mkt/toc/v1/apply/batchApply",
    "claimSevenDayLottery": "/zeekrlife-mp-mkt/toc/v1/applyV2/apply",
    "claimWalk": "/zeekrlife-mp-val/v1/carEnergy/collectedAllEnergy",
    "claimIntegral": "/zeekrlife-mp-val/v1/carEnergy/collectIntegralZeekrBalls",
}

VAL_DEBRIS = "DEBRIS"
VAL_WALK = "CARBON_VALUE"
VAL_INTEGRAL = "ZEEKR_VALUE"
SCENE_SEVEN_DAY_LOTTERY = "SIGN_CONTINUOUS_7_LOTTERY"
RECORD_SEVEN_DAY_LOTTERY = "zgreen_7day_activity"

TASK_ARTICLE = "阅读文章"
TASK_WALK = "步行3000步"
TASK_PRAISE = "每周点赞帖子"

CST = timezone(timedelta(hours=8))


# ─────────────────────────── 纯工具函数（无副作用，便于单测） ───────────────────────────


def cst_now_str() -> str:
    """北京时间字符串（不依赖宿主时区设置）。"""
    d = datetime.now(CST)
    return f"{d.year}/{d.month}/{d.day} {d.strftime('%H:%M:%S')}"


def cst_date_str(ms: int) -> str:
    """毫秒时间戳 → 北京时间的 年/月/日。"""
    d = datetime.fromtimestamp(ms / 1000, CST)
    return f"{d.year}/{d.month}/{d.day}"


def clean_token(raw: Any) -> str:
    """清洗用户粘进来的 Token。

    兼容：带单/双引号、前后空格、零宽字符、`Bearer ` 大小写、
    只粘裸 JWT（自动补 `Bearer `）、以及没替换掉的占位符（如 ${TOKEN} / <粘贴Token>）。

    返回空串表示「这里没有可用的 Token」——调用方应按未配置处理。
    """
    s = "" if raw is None else str(raw)
    for ch in ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff", "\u2060", "\u00a0"):
        s = s.replace(ch, " ")
    s = s.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    s = s.strip().strip("\"'").strip()
    idx = s.lower().find("bearer")
    if idx > 0:
        s = s[idx:]
    s = " ".join(s.split())
    if s and not s.lower().startswith("bearer") and s.startswith("eyJ") and s.count(".") >= 2:
        s = "Bearer " + s
    # 极氪 Token 一定是 JWT（脚本要用里面的 accountId / deviceId）
    if not s:
        return ""
    head = "Bearer "
    token = s[len(head):] if s.lower().startswith("bearer ") else ""
    token = token.strip()
    parts = token.split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return ""
    return "Bearer " + token


def token_from_store(raw: Any) -> str:
    """从常见极氪脚本的存储值里取出 Token。

    兼容 `{"authorization":"Bearer eyJ..."}`（键 zeekr_val / ZEEKR_VAL）、
    `authorization=Bearer%20eyJ...`、以及裸 Token。
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s) or {}
        except Exception:
            return ""
        for key in ("authorization", "Authorization", "token", "TOKEN", "ZEEKR_TOKEN"):
            if obj.get(key):
                return clean_token(obj[key])
        return ""
    if "authorization=" in s.lower():
        m = s.lower().index("authorization=")
        s = s[m + len("authorization="):].split("&")[0].split("\n")[0]
        s = urllib.parse.unquote(s)
    return clean_token(s)


def parse_token(token: str) -> Dict[str, Any]:
    """解析 JWT：账号 ID、设备 ID、过期时间。"""
    out: Dict[str, Any] = {"accountId": "", "deviceId": "", "exp": 0, "daysLeft": 999}
    try:
        payload_part = token.replace("Bearer ", "").replace("Bearer", "").strip().split(".")[1]
        pad = "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part + pad).decode("utf-8", "replace"))
    except Exception:
        return out
    if not isinstance(payload, dict):
        return out
    sub_raw = payload.get("sub")
    sub: Dict[str, Any] = {}
    if isinstance(sub_raw, str):
        try:
            sub = json.loads(sub_raw)
        except Exception:
            sub = {}
    elif isinstance(sub_raw, dict):
        sub = sub_raw
    flat = sub_raw if isinstance(sub_raw, str) else json.dumps(sub, ensure_ascii=False)
    account_id = ""
    m = re.search(r'"accountId"\s*:\s*"?(\d{10,})', flat or "")
    if m:
        account_id = m.group(1)
    if not account_id:
        account_id = str((sub.get("accountInfoDTO") or {}).get("accountId") or "")
    device_id = ""
    m2 = re.search(r'"lastLoginDeviceId"\s*:\s*"([^"]+)"', flat or "")
    if m2:
        device_id = m2.group(1)
    if not device_id:
        device_id = str((sub.get("accountLoginInfoDTO") or {}).get("lastLoginDeviceId") or "")
    exp = int((payload.get("exp") or 0) * 1000)
    days_left = int((exp - time.time() * 1000) // 86400000) if exp else 999
    return {"accountId": account_id, "deviceId": device_id, "loginDeviceId": device_id, "exp": exp, "daysLeft": days_left}


def b64e(text: str) -> str:
    """UTF-8 → 标准 base64（带 = 填充），与手机端脚本的手写实现一致。"""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def encode_step_secret(step_counts: int, layers: int = 5) -> str:
    """拼步数上报的 secret：`<步数>_salt` 连续 base64 5 层（与 App 抓包一致）。"""
    out = f"{step_counts}_salt"
    for _ in range(layers or 5):
        out = b64e(out)
    return out


def random_string(length: int) -> str:
    """随机串（nonce 用），字符集与手机端脚本一致。"""
    chars = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz1234567890"
    return "".join(random.choice(chars) for _ in range(length))


def sign_params(timestamp_ms: int, nonce: str) -> Dict[str, str]:
    """生成 x_ca_sign 及配套头（排序拼接后 SHA1）。"""
    sign = hashlib.sha1(
        "".join(sorted([ZEEKR_SECRET, nonce, str(timestamp_ms)])).encode("utf-8")
    ).hexdigest()
    return {"timestamp": str(timestamp_ms), "nonce": nonce, "sign": sign}


def parse_cron_entries(text: Any) -> List[Tuple[str, str]]:
    """解析多行「cron 表达式 + 可选场次名」。

    每行形如 `1 0 * * * 凌晨场`（前 5 段是 cron，其余是场次名，可省略）；
    也支持用 `;` 分隔。返回 [(cron, tag), ...]。
    """
    out: List[Tuple[str, str]] = []
    if text is None:
        return out
    raw = str(text).replace(";", "\n")
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) < 5:
            continue
        cron = " ".join(parts[:5])
        tag = " ".join(parts[5:]).strip()
        out.append((cron, tag))
    return out


def parse_waits(text: Any) -> List[int]:
    """解析复查间隔（秒，逗号分隔）→ 毫秒列表。"""
    out: List[int] = []
    for part in str(text or "").split(","):
        try:
            n = float(part.strip())
        except Exception:
            continue
        if n > 0:
            out.append(int(round(n * 1000)))
    return out or [45000, 60000, 75000, 60000]


def _int_or(value: Any, default: int) -> int:
    """把配置值转成整数；空值/非法值用默认值（0 是合法值，不当成空）。"""
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except Exception:
        return default


def task_done(task: Optional[Dict[str, Any]]) -> bool:
    """任务是否已完成（currentComplete >= maxCompleteLimit）。"""
    if not task:
        return True
    dto = task.get("taskTakeDTO") or {}
    return int(dto.get("currentComplete") or 0) >= int(dto.get("maxCompleteLimit") or 1)


def task_line(task: Dict[str, Any]) -> str:
    """任务一行摘要：✅/⏳ + 任务名。"""
    return ("✅ " if task_done(task) else "⏳ ") + str(task.get("name") or "")


def fail_reason(record: Any, fallback: str = "") -> str:
    """从服务端返回里取失败原因（字段名不固定，都试一遍）—— 绝不把原因丢掉。

    2026-09-21 加：领取失败的原因以前被整段丢弃，导致「界面写已领完、App 里还在」查不出来。
    """
    if not isinstance(record, dict):
        return fallback or (str(record)[:160] if record else "")
    for key in ("msg", "message", "errorMsg", "remark"):
        value = record.get(key)
        if value:
            return str(value)
    code = record.get("code")
    if isinstance(code, str) and code:
        return code
    return fallback or str(record)[:160]


def debris_name(record: Dict[str, Any]) -> str:
    """碎片奖励的可读名字。"""
    snap = ((record or {}).get("invoice") or {}).get("materialSnapshot") or {}
    name = snap.get("name") or "碎片"
    fragment = (snap.get("medalTemplateSnapshot") or {}).get("name") or ""
    return f"{name}({fragment})" if fragment else name


# ───────────────────────────────── 插件主体 ─────────────────────────────────


class ZeekrCheckin(_PluginBase):
    """极氪 App 每日自动签到（签到 / 步数 / 任务 / 领取奖励）。

    - Token：用户在手机上抓一次后填进插件设置（插件不抓取、不登录）。
    - 定时：主签到与「只领取」两组 cron 都可自定义，默认对齐手机端三场 + 每场 10 分钟补领。
    - 通知：走 MoviePilot 的通知渠道（post_message）。
    """

    plugin_name = "极氪签到"
    plugin_desc = (
        "极氪 App 每日自动签到 / 上报步数 / 阅读文章 / 每周点赞 / 领取碎片·能量球·极值。"
        "Token 由手机抓取后填入，定时可自定义。"
    )
    plugin_icon = ICON_URL
    plugin_version = "1.4.0"
    plugin_author = "m20104600"
    author_url = "https://github.com/m20104600"
    plugin_config_prefix = "zeekrcheckin_"
    plugin_order = 50
    auth_level = 1

    # 运行期状态（init_plugin 会重建；不要在导入期做任何 IO）
    _enabled: bool = False
    _run_now: bool = False
    _tokens: List[str] = []
    _sign_crons: List[Tuple[str, str]] = []
    _claim_crons: List[Tuple[str, str]] = []
    _steps: str = "10000"
    _poll: bool = False
    _notify: bool = True
    _verbose: bool = False
    _app_version: str = ""
    _like: bool = False
    _waits: str = "45,60,75"
    _settle: int = 180
    _max: int = 600
    _running: bool = False
    _stop: bool = False
    _thread: Optional[threading.Thread] = None
    _lock = threading.Lock()

    # ────────────────────────────── 生命周期 ──────────────────────────────

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读取配置；必须允许重复调用（MP 会在重载/保存时反复调用）。"""
        config = config or {}
        self._stop = False
        self._enabled = bool(config.get("enabled"))

        tokens: List[str] = []
        for line in str(config.get("token") or "").replace("&", "\n").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cleaned = clean_token(line) if "eyJ" in line or line.lower().startswith("bearer") else ""
            if not cleaned:
                cleaned = token_from_store(line)
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)
        self._tokens = tokens

        self._sign_crons = parse_cron_entries(config.get("cron_sign"))
        self._claim_crons = parse_cron_entries(config.get("cron_claim"))
        self._steps = str(config.get("steps") or "10000").strip()
        self._poll = bool(config.get("poll"))
        self._notify = bool(config.get("notify", True))
        self._verbose = bool(config.get("verbose"))
        self._app_version = str(config.get("appver") or "").strip()
        self._device_id = str(config.get("device_id") or "22662488723687344953").strip()
        self._like = bool(config.get("like"))
        self._waits = str(config.get("waits") or "45,60,75")
        self._settle = _int_or(config.get("settle"), 180)
        self._max = _int_or(config.get("max"), 600)
        self._run_now = bool(config.get("run_now"))

        # 「立即运行」开关：保存配置时跑一次，然后立刻把开关落回 off
        # （不然 MP 重启/重载读到 True 又会再跑一次）
        if self._run_now:
            self._run_now = False
            self._consume_run_now(config)
            self._start_background_run(mode="all", tag="手动")

    def _consume_run_now(self, config: Dict[str, Any]) -> None:
        """把「立即运行」开关写回关闭状态（持久化，失败只记日志）。"""
        try:
            saved = dict(config)
            saved["run_now"] = False
            self.update_config(saved)
        except Exception as error:
            logger.error(f"极氪签到：复位「立即运行」开关失败（会多跑一次）：{str(error)}")

    def _start_background_run(self, mode: str = "all", tag: str = "手动") -> bool:
        """开一个后台线程跑一次签到（不阻塞宿主）。已有任务在跑时返回 False。"""
        if self._running:
            logger.warning("极氪签到：上一次还在跑，「立即运行」本次跳过")
            if self._notify:
                try:
                    self.post_message(
                        title=f"⚠️ 极氪签到未执行（{tag}）",
                        text="上一次任务还在跑，本次跳过。等它跑完再点一次「立即运行一次」即可。",
                    )
                except Exception as error:
                    logger.error(f"极氪签到：通知发送失败：{str(error)}")
            return False
        self._thread = threading.Thread(
            target=self.run_checkin, kwargs={"mode": mode, "tag": tag}, daemon=True
        )
        self._thread.start()
        return True

    def get_state(self) -> bool:
        """插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """停用插件：打断正在跑的签到循环，释放后台资源。"""
        self._stop = True
        self._enabled = False
        # 等正在运行的循环退出（最多 5 秒，不让停用/重载卡住宿主）
        deadline = time.time() + 5
        while self._running and time.time() < deadline:
            time.sleep(0.2)
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    # ───────────────────────────── 页面与接口 ─────────────────────────────

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册「立即执行一次」接口（默认 auth=apikey）。"""
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "立即执行一次极氪签到",
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页：Token + 两组自定义 cron + 运行参数。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "verbose", "label": "输出明细日志"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "run_now",
                                            "label": "立即运行一次",
                                            "hint": "打开并保存后马上按「全流程」跑一次，跑完自动关掉",
                                            "persistent-hint": True,
                                            "color": "primary",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "Token 怎么来：手机上用 Egern / QX / Loon / Stash 的抓取规则"
                                                "（或自己抓包，域名 api-gw-toc.zeekrlife.com）打开一次极氪 App，"
                                                "把通知里那一整行 Bearer eyJ... 复制到这里。"
                                                "本插件只读取 Token，不抓取、不模拟登录。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "token",
                                            "label": "极氪 Token（每行一个账号，可留空但签到会失败）",
                                            "rows": 3,
                                            "placeholder": "Bearer eyJhbGciOi...",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cron_sign",
                                            "label": "签到定时（每行一条：5 位 cron + 可选场次名）",
                                            "rows": 3,
                                            "placeholder": "1 0 * * * 凌晨场",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cron_claim",
                                            "label": "补领定时（只领取，可留空：奖励延迟入账时补一手）",
                                            "rows": 3,
                                            "placeholder": "10 0 * * * 凌晨补领",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "steps",
                                            "label": "上报步数",
                                            "placeholder": "10000，或 off 跳过",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "device_id",
                                            "label": "请求设备 ID（从手机抓包的 device_id 填写）",
                                            "placeholder": "22662488723687344953",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "appver",
                                            "label": "App 版本（留空自动查询）",
                                            "placeholder": "5.0.5",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "poll",
                                            "label": "领取阶段轮询到领完（耗时 3~5 分钟）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "like",
                                            "label": "强制点一次赞（排查用）",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "run_now": False,
            "notify": True,
            "verbose": False,
            "token": "",
            "cron_sign": "1 0 * * * 凌晨场\n10 8 * * * 早间场\n30 21 * * * 晚间场",
            "cron_claim": "10 0 * * * 凌晨补领\n20 8 * * * 早间补领\n40 21 * * * 晚间补领",
            "steps": "10000",
            "device_id": "22662488723687344953",
            "appver": "",
            "poll": False,
            "like": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """详情页：最近一次运行结果 + Token 状态。"""
        blocks: List[dict] = []
        last = self.get_data("last_result") or {}
        if self._tokens:
            info = parse_token(self._tokens[0])
            exp = cst_date_str(info["exp"]) if info["exp"] else "未知"
            left = info["daysLeft"]
            note = f"账号 {info['accountId'] or '未知'}｜Token 过期 {exp}（剩余 {left} 天）"
            if left <= 0:
                note += "｜❌ 已过期，请重新抓取"
            elif left < 7:
                note += "｜⚠️ 即将过期"
        else:
            note = "❌ 还没有配置 Token（去配置页粘贴 Bearer eyJ...）"
        blocks.append(
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": note},
            }
        )
        if last:
            text = (
                f"{last.get('time', '')}（{last.get('tag') or '手动'}）\n"
                + "\n".join(last.get("lines") or [])
            )
            blocks.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "success" if last.get("ok") else "error",
                        "variant": "tonal",
                        "text": text,
                    },
                }
            )
        else:
            blocks.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "还没有运行记录。启用后按定时执行，或在配置页打开「立即运行一次」开关手动跑，也可以调用 /api/v1/plugin/ZeekrCheckin/run。",
                    },
                }
            )
        return blocks

    def get_service(self) -> List[Dict[str, Any]]:
        """把配置里的两组 cron 注册成定时服务。"""
        if not self.get_state():
            return []
        services: List[Dict[str, Any]] = []
        for mode, entries in (("all", self._sign_crons), ("claim", self._claim_crons)):
            for idx, (cron, tag) in enumerate(entries, start=1):
                try:
                    trigger = CronTrigger.from_crontab(cron)
                except Exception as error:
                    logger.error(
                        f"极氪签到：cron「{cron}」无效，已跳过这一条（{str(error)}）"
                    )
                    continue
                label = tag or f"{'签到' if mode == 'all' else '补领'}场次{idx}"
                services.append(
                    {
                        "id": f"ZeekrCheckin.{mode}.{idx}",
                        "name": f"极氪签到·{label}",
                        "trigger": trigger,
                        "func": self.run_checkin,
                        "kwargs": {"mode": mode, "tag": tag or label},
                    }
                )
        return services

    def api_run(self, mode: str = "all") -> Dict[str, Any]:
        """立即执行一次（后台线程，接口立刻返回）。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if self._running:
            return {"success": False, "message": "上一次仍在运行中"}
        if mode not in ("all", "sign", "claim"):
            return {"success": False, "message": "mode 只能是 all / sign / claim"}
        if not self._start_background_run(mode=mode, tag="手动"):
            return {"success": False, "message": "上一次仍在运行中"}
        return {"success": True, "message": "已触发，结果稍后见通知与插件详情页"}

    # ───────────────────────────── 主流程 ─────────────────────────────

    def run_checkin(self, mode: str = "all", tag: str = "") -> Dict[str, Any]:
        """一次完整执行：对配置里的每个账号跑一遍，并发送通知。

        :param mode: all=签到+任务+领取，sign=只签到做任务，claim=只领取
        :param tag: 场次名，只影响通知标题
        """
        if not self._lock.acquire(blocking=False):
            logger.info("极氪签到：上一次还在跑，本次跳过")
            return {"ok": False, "lines": ["上一次还在跑，本次跳过"]}
        self._running = True
        self._stop = False
        try:
            if not self._tokens:
                title = "❌ 极氪签到失败" + (f"（{tag}）" if tag else "")
                lines = ["❌ 缺少 Token：请在插件配置页填入 Bearer eyJ...（手机抓一次即可）"]
                self._notify_result(title, lines, tag)
                return {"ok": False, "lines": lines}

            lines: List[str] = []
            ok = True
            warn = False
            for i, token in enumerate(self._tokens, start=1):
                if len(self._tokens) > 1:
                    lines.append(f"—— 账号 {i} ——")
                result = self._run_one(token, mode=mode, tag=tag)
                lines.extend(result["lines"])
                ok = ok and result["ok"]
                warn = warn or bool(result.get("warn"))
                if self._stop:
                    lines.append("⏹ 插件被停用/重载，本次提前结束")
                    break

            if not ok:
                title = "❌ 极氪签到失败"
            elif warn:
                # 签到本身成功，但有奖励没领到 —— 标题必须能一眼看出来（2026-09-21）
                title = "⚠️ 极氪签到（未领净）"
            else:
                title = "✅ 极氪签到"
            title += f"（{tag}）" if tag else ""
            self._notify_result(title, lines, tag)
            self.save_data(
                "last_result",
                {
                    "time": cst_now_str(),
                    "tag": tag,
                    "mode": mode,
                    "ok": ok,
                    "warn": warn,
                    "lines": lines,
                },
            )
            return {"ok": ok, "lines": lines}
        finally:
            self._running = False
            self._lock.release()

    def _run_one(self, token: str, mode: str, tag: str) -> Dict[str, Any]:
        """跑单个账号（移植自手机端脚本的 zeekrMain）。"""
        lines: List[str] = []
        ok = True

        def out(msg: str) -> None:
            lines.append(msg)
            logger.info(f"[极氪签到] {msg}")

        def vlog(msg: str) -> None:
            if self._verbose:
                logger.info(f"[极氪签到·明细] {msg}")

        info = parse_token(token)
        if not info["loginDeviceId"]:
            return {
                "ok": False,
                "lines": ["❌ JWT 里没有登录设备 ID，Token 可能不完整"],
            }
        app_version = self._app_version or self._fetch_app_version()
        out(f"{cst_now_str()} | 账号: {info['accountId']} | 客户端: MoviePilot v3")
        out(
            "Token 过期: "
            + (cst_date_str(info["exp"]) if info["exp"] else "未知")
            + f" (剩余 {info['daysLeft']} 天)"
        )
        if info["daysLeft"] <= 0:
            return {"ok": False, "lines": lines + ["❌ Token 已过期，请重新抓包更新"]}
        if info["daysLeft"] < 7:
            out("⚠️ Token 即将过期，请尽快更新！")
        vlog(f"App 版本: v{app_version}")

        ctx = {
            "token": token,
            "accountId": info["accountId"],
            "deviceId": self._device_id,
            "appVersion": app_version,
        }

        steps_raw = self._steps
        step_off = steps_raw.lower() in ("off", "0", "no", "false")
        steps = 0 if step_off else (int(steps_raw) if steps_raw.isdigit() else random.randint(8000, 12000))

        def push_steps() -> bool:
            time.sleep(random.uniform(1.2, 2.2))
            return self._sync_walk(ctx, steps, out=out)

        if mode in ("all", "sign"):
            if not self._sign_in(ctx, out):
                return {"ok": False, "lines": lines + ["❌ 签到接口报错，本次中止"]}
            if not step_off:
                push_steps()
            time.sleep(random.uniform(1.2, 2.2))
            tasks = self._do_tasks(ctx, out, vlog)
            if tasks:
                vlog("📋 任务(初查): " + "  ".join(task_line(t) for t in tasks))

        result = {"debrisCount": 0, "lotteryCount": 0, "walkVal": 0, "integralVal": 0, "rounds": 0, "failed": []}
        if mode in ("all", "claim"):
            time.sleep(random.uniform(2.0, 3.5))
            result = self._claim_all(ctx, out, vlog, poll=self._poll)

        if mode in ("all", "sign"):
            final_tasks = self._get_tasks(ctx, out)
            if final_tasks:
                out("📋 任务: " + "  ".join(task_line(t) for t in final_tasks))
            walk_task = next((t for t in final_tasks if t.get("name") == TASK_WALK), None)
            if not step_off and mode == "all" and walk_task and not task_done(walk_task):
                out("🔁 「步行3000步」仍未达标：补报步数后重新扫描")
                push_steps()
                again = self._claim_all(
                    ctx,
                    out,
                    vlog,
                    poll=self._poll,
                    settle=min(self._settle, 120),
                )
                result = {
                    "debrisCount": result["debrisCount"] + again["debrisCount"],
                    "lotteryCount": result.get("lotteryCount", 0) + again.get("lotteryCount", 0),
                    "walkVal": result["walkVal"] + again["walkVal"],
                    "integralVal": result["integralVal"] + again["integralVal"],
                    "rounds": result["rounds"] + again["rounds"],
                    "failed": again.get("failed") or [],
                }

        out(
            f"🏁 本次领取: 碎片 {result['debrisCount']} 个, 七日奖励 {result.get('lotteryCount', 0)} 个, "
            f"能量球 +{result['walkVal']}, 极值 +{result['integralVal']}"
        )
        failures = result.get("failed") or []
        if failures:
            out(
                "⚠️ 未领净 {} 项（下一条补领会再试）：{}".format(
                    len(failures),
                    "；".join(f"{f.get('label')}→{f.get('reason')}" for f in failures),
                )
            )
        else:
            out("🎉 全部完成！")
        return {"ok": ok, "warn": bool(failures), "lines": lines}

    # ───────────────────────────── 请求层 ─────────────────────────────

    def _headers(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        """构造极氪接口请求头（含 x_ca_sign 签名）。"""
        timestamp = int(time.time() * 1000)
        nonce = random_string(15)
        sig = sign_params(timestamp, nonce)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Authorization": ctx["token"],
            "x_ca_key": "H5-SIGN-SECRET-KEY",
            "x_ca_nonce": sig["nonce"],
            "x_ca_timestamp": sig["timestamp"],
            "x_ca_sign": sig["sign"],
            "WorkspaceId": "prod",
            "Version": "2",
            "app_type": "h5",
            "app_code": "toc_h5_green_zeekrapp",
            "platform": "",
            "platform_h5": "IOS",
            "risk_platform": "h5",
            "riskTimeStamp": sig["timestamp"],
            "riskVersion": "1",
            "device_id": ctx.get("deviceId") or "",
            "x_gray_code": "gray45",
            "AppId": "ONEX97FB91F061405",
            "X-CORS-ONEX97FB91F061405-prod": "1",
            "Eagleeye-Sessionid": "",
            "Eagleeye-Traceid": "",
            "Origin": "https://activity-h5.zeekrlife.com",
            "Referer": "https://activity-h5.zeekrlife.com/",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
                f"(KHTML, like Gecko) zeekr_iOS_v{ctx.get('appVersion') or DEFAULT_APP_VERSION}"
            ),
        }

    def _request(
        self, ctx: Dict[str, Any], path: str, method: str = "GET", body: Optional[dict] = None
    ) -> Dict[str, Any]:
        """请求极氪接口并解析 JSON；任何异常都返回带 code 的字典，不抛给宿主。"""
        data = None
        if method != "GET":
            data = json.dumps(body if body is not None else {}).encode("utf-8")
        req = urllib.request.Request(
            ZEEKR_BASE + path, data=data, headers=self._headers(ctx), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            try:
                text = error.read().decode("utf-8", "replace")
            except Exception:
                return {"code": "HTTP_ERROR", "msg": f"HTTP {error.code}"}
        except Exception as error:  # 网络 / TLS / 超时
            return {"code": "NETWORK_ERROR", "msg": str(error)}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"code": "PARSE_ERROR", "msg": str(text)[:200]}
        except Exception:
            return {"code": "PARSE_ERROR", "msg": text[:200]}

    def _get(self, ctx: Dict[str, Any], path: str) -> Dict[str, Any]:
        """GET 极氪接口。"""
        return self._request(ctx, path, "GET")

    def _post(self, ctx: Dict[str, Any], path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        """POST 极氪接口。"""
        return self._request(ctx, path, "POST", body)

    def _fetch_app_version(self) -> str:
        """查 iTunes 拿极氪 App 版本号；失败退回默认值。"""
        try:
            req = urllib.request.Request(ITUNES_LOOKUP, headers={})
            with urllib.request.urlopen(req, timeout=8, context=ssl.create_default_context()) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return str(((data.get("results") or [{}])[0] or {}).get("version") or DEFAULT_APP_VERSION)
        except Exception:
            return DEFAULT_APP_VERSION

    # ───────────────────────────── 业务动作 ─────────────────────────────

    def _sign_in(self, ctx: Dict[str, Any], out) -> bool:
        """每日签到。"""
        data = self._post(ctx, ZEEKR_API["signIn"], {})
        if data.get("code") == "000000":
            info = (data.get("data") or {}).get("signInZgreenInfo") or []
            today = None
            streak = None
            for item in info:
                if item.get("taskName") == "每日签到":
                    today = item
                elif str(item.get("taskName") or "").startswith("连续签到"):
                    streak = item
            out("✅ 签到成功" + ("（今日已签到）" if today and today.get("taskStatus") else ""))
            if streak:
                out("🔥 " + str(streak.get("taskName")))
            return True
        out("❌ 签到失败: " + str(data.get("msg") or data))
        return False

    def _sync_walk(
        self, ctx: Dict[str, Any], steps: int, source_type: int = 20, out=None
    ) -> bool:
        """上报步数（打通「步行3000步」任务）。"""

        def say(msg: str) -> None:
            (out or (lambda m: logger.info(f"[极氪签到] {m}")))(msg)

        base = {
            "stepCounts": steps,
            "stepCountsSecret": encode_step_secret(steps),
            "sourceType": source_type,
        }
        body = dict(base)
        if ctx.get("accountId"):
            body["accountId"] = ctx["accountId"]
        data = self._post(ctx, ZEEKR_API["walkData"], body)
        if data.get("code") != "000000" and ctx.get("accountId"):
            say(f"⚠️ 步数上报带 accountId 失败（{data.get('code')}），改用不带 accountId 重试")
            data = self._post(ctx, ZEEKR_API["walkData"], base)
        if data.get("code") == "000000":
            say(f"🚶 步数上报成功: {steps} 步")
            return True
        say(f"❌ 步数上报失败: {data.get('msg') or data}")
        return False

    def _get_tasks(self, ctx: Dict[str, Any], out=None) -> List[Dict[str, Any]]:
        """拉任务列表。"""
        data = self._post(
            ctx,
            ZEEKR_API["taskMsg"],
            {"activityRecord": "medal_compose_task_manage", "optional": {"fetchTaskTakeAndReachTimesInfo": True}},
        )
        if data.get("code") != "000000":
            if out:
                out("❌ 查询任务列表失败: " + str(data.get("msg") or data))
            return []
        return (data.get("data") or {}).get("taskReachMsgList") or []

    def _read_article(self, ctx: Dict[str, Any], tasks: List[Dict[str, Any]], out) -> Optional[bool]:
        """做「阅读文章」任务：打开任务里给出的文章详情。"""
        task = next((t for t in tasks if t.get("name") == TASK_ARTICLE), None)
        if not task:
            return None
        url = ((task.get("doc") or {}).get("path") or "")
        article_id = ""
        for part in url.replace("?", "&").split("&"):
            if part.startswith("acticleId="):
                article_id = part[len("acticleId="):]
        if not article_id:
            out("❌ 阅读文章: 任务里没拿到文章 ID")
            return None
        data = self._get(ctx, ZEEKR_API["articleDetail"] + "?id=" + article_id)
        if data.get("code") == "000000":
            out("📖 阅读文章成功: " + str((data.get("data") or {}).get("title") or article_id))
            return True
        out("❌ 阅读文章失败: " + str(data.get("msg") or data))
        return None

    def _like_square_post(self, ctx: Dict[str, Any], out) -> Optional[bool]:
        """做「每周点赞帖子」任务：广场里找一条没点过的点赞。"""
        target = None
        for page in (1, 2, 3):
            data = self._get(
                ctx, ZEEKR_API["squareList"] + f"?pageNo={page}&pageSize=20&sort=0"
            )
            items = ((data or {}).get("data") or {}).get("list") or []
            for item in items:
                if item and item.get("isFabulous") == 0 and item.get("id") and item.get("accountId"):
                    target = item
                    break
            if target or not items:
                break
        if not target:
            out("⚠️ 广场列表里没有可点赞的帖子（前三页都点过了）")
            return None
        data = self._post(
            ctx,
            ZEEKR_API["fabulous"],
            {
                "moduleId": str(target["id"]),
                "accountId": ctx["accountId"],
                "fabulousState": 1,
                "moduleType": 1,
                "toAccountId": str(target["accountId"]),
                "invitationType": 2,
            },
        )
        if data.get("code") == "000000":
            title = target.get("title")
            label = "《" + str(title)[:18] + "》" if title else "帖子 " + str(target["id"])[-6:]
            out("👍 社区点赞成功: " + label)
            return True
        out("❌ 社区点赞失败: " + str(data.get("msg") or data))
        return None

    def _do_tasks(self, ctx: Dict[str, Any], out, vlog) -> List[Dict[str, Any]]:
        """阅读文章 + 每周点赞（已完成则跳过）。"""
        tasks = self._get_tasks(ctx, out)
        if not tasks:
            out("⚠️ 未取到任务列表（跳过做任务）")
            return []
        self._read_article(ctx, tasks, out)
        weekly = next((t for t in tasks if t.get("name") == TASK_PRAISE), None)
        if not self._like and task_done(weekly):
            out("👍 每周点赞帖子: 本周已完成，跳过")
        else:
            self._like_square_post(ctx, out)
        return tasks

    def _get_uncollected(self, ctx: Dict[str, Any], out, vlog) -> Dict[str, List[dict]]:
        """查询可领取奖励：碎片 / 能量球 / 极值。"""
        data = self._post(ctx, ZEEKR_API["uncollected"], {"accountId": ctx.get("accountId") or ""})
        empty = {"debrisList": [], "walkList": [], "integralList": [], "lotteryList": []}
        if data.get("code") != "000000":
            out("❌ 查询可领取奖品失败: " + str(data.get("msg") or data))
            return empty
        items = (data.get("data") or {}).get("uncollectedVal") or []
        result = {"debrisList": [], "walkList": [], "integralList": [], "lotteryList": []}
        for item in items:
            code = (item or {}).get("valDefineCode")
            scene = (item or {}).get("sceneCode")
            if scene == SCENE_SEVEN_DAY_LOTTERY:
                result["lotteryList"].append(item)
            elif code == VAL_DEBRIS:
                result["debrisList"].append(item)
            elif code == VAL_WALK:
                result["walkList"].append(item)
            elif code == VAL_INTEGRAL:
                result["integralList"].append(item)
        summary = (
            f"{len(result['debrisList'])} 个碎片, {len(result['lotteryList'])} 个七日抽奖球, "
            f"{len(result['walkList'])} 个能量球, {len(result['integralList'])} 个极值"
        )
        if items:
            out("📦 可领取: " + summary)
        else:
            vlog("📦 可领取: " + summary)
        return result

    def _claim_debris(self, ctx: Dict[str, Any], debris_list: List[dict], out) -> Dict[str, Any]:
        """批量领取碎片（与 App 一致：一次 batchApply 提交全部）；失败则**真的**逐个重试。

        ⚠️ 2026-09-21 修（真事故）：旧版遇到「批量里有 1 个失败」时只打了一句
        「逐个重试」的日志、实际没有重试，失败原因还被整个丢掉 —— 结果日志写
        「✅ 可取皆已领完」，App 里那张碎片还在，用户只能手动领。
        现在返回 {"claimed": [...], "failed": [{"id","label","reason"}]}，
        由 _claim_all 决定「下一轮再试」还是「进通知」。
        """
        if not debris_list:
            return {"claimed": [], "failed": []}

        def to_cmd(item: dict) -> dict:
            return {
                "record": item.get("eventCode"),
                "payContent": {"bubbleAssetsId": item.get("id")},
                "applyExt": {"origin": item.get("sourceId")},
            }

        def label_of(item: dict) -> str:
            return str(item.get("sourceId") or item.get("sceneRemark") or item.get("id"))

        claimed: List[str] = []
        pending: List[Dict[str, Any]] = []
        data = self._post(ctx, ZEEKR_API["claimDebris"], {"applyCmdList": [to_cmd(i) for i in debris_list]})
        payload = data.get("data")
        if data.get("code") == "000000" and isinstance(payload, list):
            for index, record in enumerate(payload):
                item = debris_list[index] if index < len(debris_list) else {}
                if (record or {}).get("success"):
                    claimed.append(debris_name(record))
                else:
                    pending.append(
                        {"item": item, "reason": fail_reason(record, str(data.get("msg") or ""))}
                    )
            if pending:
                out(f"⚠️ 批量领取有 {len(pending)} 个失败，改为逐个重试")
        else:
            out(f"⚠️ 批量领取碎片失败（{data.get('msg') or data}），改为逐个领取")
            pending = [
                {"item": item, "reason": str(data.get("msg") or "批量接口返回异常")}
                for item in debris_list
            ]

        failed: List[Dict[str, Any]] = []
        for entry in pending:
            item, reason = entry["item"], entry["reason"]
            one = self._post(ctx, ZEEKR_API["claimDebris"], {"applyCmdList": [to_cmd(item)]})
            one_payload = one.get("data")
            ok = (
                one.get("code") == "000000"
                and isinstance(one_payload, list)
                and any((r or {}).get("success") for r in one_payload)
            )
            if ok:
                for record in one_payload:
                    if (record or {}).get("success"):
                        claimed.append(debris_name(record))
            else:
                why = fail_reason(
                    (one_payload or [None])[0], str(one.get("msg") or reason)
                )
                out(f"❌ 碎片领取失败（{label_of(item)}）: {why}")
                failed.append({"id": item.get("id"), "label": label_of(item), "reason": why})
            time.sleep(random.uniform(0.8, 1.5))
        if claimed:
            out("🧩 碎片奖励: " + "、".join(claimed))
        return {"claimed": claimed, "failed": failed}

    def _claim_seven_day_lottery(self, ctx: Dict[str, Any], lottery_list: List[dict], out) -> Dict[str, Any]:
        """领取七日连签抽奖球（含锦鲤泡泡/5Kr 等奖励）。"""
        if not lottery_list:
            return {"claimed": [], "failed": []}
        claimed: List[str] = []
        failed: List[Dict[str, Any]] = []
        for item in lottery_list:
            label = str(item.get("sourceId") or item.get("sceneRemark") or "七日连签抽奖球")
            data = self._post(
                ctx,
                ZEEKR_API["claimSevenDayLottery"],
                {
                    "record": RECORD_SEVEN_DAY_LOTTERY,
                    "fixedZgreenAssetId": item.get("id"),
                    "optional": {"mappingMsg": True},
                },
            )
            payload = data.get("data") or {}
            if data.get("code") == "000000" and payload.get("success"):
                prize = (((payload.get("invoice") or {}).get("materialSnapshot") or {}).get("name")) or "七日连签奖励"
                claimed.append(prize)
                out(f"🎁 七日连签奖励已领: {prize}")
            else:
                why = fail_reason(payload, str(data.get("msg") or "七日连签领取失败"))
                out(f"❌ 七日连签奖励领取失败（{label}）: {why}")
                failed.append({"id": item.get("id"), "label": label, "reason": why})
            time.sleep(random.uniform(1.0, 2.0))
        return {"claimed": claimed, "failed": failed}

    def _claim_walk(self, ctx: Dict[str, Any], walk_list: List[dict], out) -> Dict[str, Any]:
        """领取能量球（碳积分）。返回 {"val": int, "failed": [...]}（失败要能被重试/上报）。"""
        total = 0
        failed: List[Dict[str, Any]] = []
        for item in walk_list:
            val = int(item.get("val") or 0)
            data = self._post(ctx, ZEEKR_API["claimWalk"], {"energyIds": [item.get("id")]})
            if data.get("code") == "000000":
                total += val
                out(f"♻️ 能量球已领: +{val}")
            else:
                why = str(data.get("msg") or data)[:160]
                out(f"❌ 能量球领取失败（{val}g）: {why}")
                failed.append({"id": item.get("id"), "label": f"{val}g 能量球", "reason": why})
            time.sleep(random.uniform(0.8, 1.5))
        return {"val": total, "failed": failed}

    def _claim_integral(self, ctx: Dict[str, Any], integral_list: List[dict], out) -> Dict[str, Any]:
        """领取极值。返回 {"val": int, "failed": [...]}。"""
        total = 0
        failed: List[Dict[str, Any]] = []
        for item in integral_list:
            val = int(item.get("val") or 0)
            data = self._post(ctx, ZEEKR_API["claimIntegral"], {"energyIds": [item.get("id")]})
            if data.get("code") == "000000":
                total += val
                out(f"🏆 极值已领: +{val}")
            else:
                why = str(data.get("msg") or data)[:160]
                out(f"❌ 极值领取失败（{val}）: {why}")
                failed.append({"id": item.get("id"), "label": f"{val} 极值", "reason": why})
            time.sleep(random.uniform(0.8, 1.5))
        return {"val": total, "failed": failed}

    def _claim_all(
        self,
        ctx: Dict[str, Any],
        out,
        vlog,
        poll: bool = False,
        waits: Optional[List[int]] = None,
        settle: Optional[int] = None,
        max_seconds: Optional[int] = None,
    ) -> Dict[str, int]:
        """领取循环：先领能量球 → 领极值 → 批量领碎片 → 复查。

        顺序不能反：能量球入账会让「减碳2000g」任务达标，进而生成新碎片。
        奖励是异步入账的（1~3 分钟），所以：
          poll=False（默认）：只查一轮，延迟入账的补领交给后面那条「只领取」定时；
          poll=True：按 waits / settle / max 做时间窗口轮询，一轮领光。
        """
        waits_ms = waits or parse_waits(self._waits)
        settle_s = self._settle if settle is None else settle
        max_s = self._max if max_seconds is None else max_seconds
        silent_rounds = 3
        max_attempts = 3  # 同一项最多尝试几次（失败项留到下一轮再试，不再"记成已领"）
        started = time.time()
        claimed: Dict[Any, int] = {}
        attempts: Dict[Any, int] = {}
        failed: Dict[Any, Dict[str, Any]] = {}
        debris_count = walk_val = integral_val = lottery_count = 0
        empty_streak = 0
        rounds = 0
        last_claim_at = 0.0
        conclusion = ""

        def fresh(items: List[dict]) -> List[dict]:
            return [i for i in items if i.get("id") not in claimed]

        def bump(items: List[dict]) -> List[dict]:
            """返回本轮还能尝试的项（每项最多 max_attempts 次），并累加尝试次数。"""
            todo: List[dict] = []
            for item in items:
                key = item.get("id")
                n = attempts.get(key, 0) + 1
                attempts[key] = n
                if n <= max_attempts:
                    todo.append(item)
            return todo

        def settle(items: List[dict], fail_list: List[Dict[str, Any]]) -> None:
            """只把**确实领到**的项记进 claimed —— 旧版领之前就记，失败后永不重试。"""
            for item in items:
                if not any(f.get("id") == item.get("id") for f in fail_list):
                    claimed[item.get("id")] = 1
                    failed.pop(item.get("id"), None)

        while True:
            rounds += 1
            got = self._get_uncollected(ctx, out, vlog)
            d, w, g = fresh(got["debrisList"]), fresh(got["walkList"]), fresh(got["integralList"])
            l = fresh(got["lotteryList"])
            elapsed = time.time() - started
            if self._stop:
                conclusion = "⏹ 插件停用，领取中断"
                break

            w_try, g_try, d_try, l_try = bump(w), bump(g), bump(d), bump(l)

            if not d_try and not w_try and not g_try and not l_try:
                if d or w or g or l:
                    # 列表里还有东西，但都重试到上限了 —— 绝不能报「已领完」
                    for item in list(d) + list(w) + list(g) + list(l):
                        failed.setdefault(
                            item.get("id"),
                            {
                                "id": item.get("id"),
                                "label": str(item.get("sourceId") or item.get("sceneRemark") or item.get("id")),
                                "reason": f"重试 {max_attempts} 次仍未领到",
                            },
                        )
                    conclusion = f"⚠️ 仍有 {len(failed)} 项未领到（每项已重试 {max_attempts} 次）"
                    break
                empty_streak += 1
                since_claim = time.time() - (last_claim_at or started)
                if not poll or (empty_streak >= silent_rounds and since_claim >= settle_s):
                    if failed:
                        conclusion = "⚠️ 复查结束，但仍有 {} 项领取失败：{}".format(
                            len(failed),
                            "、".join(f"{f.get('label')}({f.get('reason')})" for f in failed.values()),
                        )
                    else:
                        conclusion = (
                            f"✅ 复查确认：可取皆已领完（共 {rounds} 轮 / {round(elapsed)}s）"
                            if poll
                            else "✅ 本次查询无待领取奖励"
                        )
                    break
                wait_try = waits_ms[max(0, min(empty_streak - 1, len(waits_ms) - 1))]
                if elapsed + wait_try / 1000 > max_s:
                    conclusion = f"⏰ 观察到 {round(elapsed)}s，到领取窗口上限，结束复查"
                    break
                vlog(f"第 {rounds} 轮暂无可领（已观察 {round(elapsed)}s）...")
            else:
                walked = self._claim_walk(ctx, w_try, out)
                walk_val += int(walked.get("val") or 0)
                settle(w_try, walked.get("failed") or [])
                for f in walked.get("failed") or []:
                    failed[f.get("id")] = f

                got_integral = self._claim_integral(ctx, g_try, out)
                integral_val += int(got_integral.get("val") or 0)
                settle(g_try, got_integral.get("failed") or [])
                for f in got_integral.get("failed") or []:
                    failed[f.get("id")] = f

                got_debris = self._claim_debris(ctx, d_try, out)
                debris_count += len(got_debris.get("claimed") or [])
                settle(d_try, got_debris.get("failed") or [])
                for f in got_debris.get("failed") or []:
                    failed[f.get("id")] = f

                got_lottery = self._claim_seven_day_lottery(ctx, l_try, out)
                lottery_count += len(got_lottery.get("claimed") or [])
                settle(l_try, got_lottery.get("failed") or [])
                for f in got_lottery.get("failed") or []:
                    failed[f.get("id")] = f

                empty_streak = 0
                last_claim_at = time.time()

            if not poll:
                conclusion = (
                    f"⚠️ 未领净 {len(failed)} 项（下一条补领会再试）"
                    if failed
                    else "✅ 已领取一轮（延迟入账的奖励由「补领」定时任务补上）"
                )
                break
            wait = waits_ms[max(0, min(empty_streak - 1, len(waits_ms) - 1))]
            if time.time() - started + wait / 1000 > max_s:
                conclusion = "⏰ 到领取窗口上限，结束复查"
                break
            vlog(f"{round(wait / 1000)}s 后复查（新奖励可能还在入账路上）...")
            time.sleep(wait / 1000.0)  # wait 是毫秒（与等待上限比较用毫秒），sleep 要秒

        if conclusion:
            out(conclusion)
        return {
            "debrisCount": debris_count,
            "lotteryCount": lottery_count,
            "walkVal": walk_val,
            "integralVal": integral_val,
            "rounds": rounds,
            "failed": list(failed.values()),
        }

    # ───────────────────────────── 通知 ─────────────────────────────

    def _notify_result(self, title: str, lines: List[str], tag: str) -> None:
        """发通知（走宿主通知渠道）；失败不影响业务流程。"""
        if not self._notify:
            return
        text = "\n".join(lines)
        try:
            self.post_message(title=title, text=text)
        except Exception as error:
            logger.error(f"极氪签到：通知发送失败：{str(error)}")
