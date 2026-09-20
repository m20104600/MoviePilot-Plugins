# -*- coding: utf-8 -*-
"""在 MoviePilot 宿主之外加载本插件的桩（宿主 SDK / APScheduler 替身）。

用途：本机没有 MoviePilot 环境时也能跑单测、跑端口一致性对照、跑真实接口冒烟。
它只提供插件 import 时需要的符号，不模拟任何业务行为。
"""

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_FILE = ROOT / "plugins.v3" / "zeekrcheckin" / "__init__.py"


class _Logger:
    """替身 logger：直接打印/收集日志。"""

    def __init__(self):
        self.records = []

    def _log(self, level, msg):
        self.records.append((level, str(msg)))
        print(f"[{level}] {msg}")

    def info(self, msg, *a, **k):
        self._log("INFO", msg)

    def warning(self, msg, *a, **k):
        self._log("WARNING", msg)

    def error(self, msg, *a, **k):
        self._log("ERROR", msg)

    def debug(self, msg, *a, **k):
        self._log("DEBUG", msg)


class _CronTrigger:
    """APScheduler CronTrigger 的最小替身：只做 5 段校验。"""

    def __init__(self, expression: str):
        self.expression = expression

    @classmethod
    def from_crontab(cls, expression: str):
        fields = str(expression).split()
        if len(fields) != 5:
            raise ValueError(f"Wrong number of fields; got {len(fields)}, expected 5")
        return cls(expression)

    def __repr__(self):
        return f"CronTrigger({self.expression})"


class FakePluginBase:
    """_PluginBase 替身：只保留插件用到的基类方法。"""

    def __init__(self, *args, **kwargs):
        self._store = {}
        self.messages = []

    def get_data_path(self, plugin_id=None):
        return ROOT / ".tmp-data"

    def save_data(self, key, value, plugin_id=None):
        self._store[key] = value

    def get_data(self, key=None, plugin_id=None):
        return self._store.get(key) if key else dict(self._store)

    def del_data(self, key, plugin_id=None):
        return self._store.pop(key, None)

    def get_config(self, plugin_id=None):
        return self._store.get("__config__") or {}

    def update_config(self, config, plugin_id=None):
        self._store["__config__"] = config
        return True

    def post_message(self, channel=None, mtype=None, title=None, text=None, image=None, link=None, **kwargs):
        self.messages.append({"title": title, "text": text, "channel": channel, "mtype": mtype})


def install_stubs():
    """把替身注册进 sys.modules（重复调用安全）。"""
    if "app" in sys.modules and getattr(sys.modules["app"], "__stub__", False):
        return

    app = types.ModuleType("app")
    app.__stub__ = True
    plugins = types.ModuleType("app.plugins")
    plugins._PluginBase = FakePluginBase
    sdk = types.ModuleType("app.sdk")
    logging_mod = types.ModuleType("app.sdk.logging")
    logging_mod.logger = _Logger()
    schemas = types.ModuleType("app.schemas")
    types_mod = types.ModuleType("app.schemas.types")

    class _EventType:
        PluginAction = "PluginAction"

    class _NotificationType:
        Plugin = "Plugin"

    types_mod.EventType = _EventType
    types_mod.NotificationType = _NotificationType

    aps = types.ModuleType("apscheduler")
    aps_triggers = types.ModuleType("apscheduler.triggers")
    aps_cron = types.ModuleType("apscheduler.triggers.cron")
    aps_cron.CronTrigger = _CronTrigger

    sys.modules.update(
        {
            "app": app,
            "app.plugins": plugins,
            "app.sdk": sdk,
            "app.sdk.logging": logging_mod,
            "app.schemas": schemas,
            "app.schemas.types": types_mod,
            "apscheduler": aps,
            "apscheduler.triggers": aps_triggers,
            "apscheduler.triggers.cron": aps_cron,
        }
    )
    app.plugins = plugins  # 让 `from app.plugins import _PluginBase` 生效
    sys.modules["app.plugins"] = plugins


def load_plugin(module_name: str = "zeekrcheckin_under_test"):
    """加载插件模块（带宿主替身）。"""
    install_stubs()
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def logger():
    """拿到替身 logger（断言日志用）。"""
    install_stubs()
    return sys.modules["app.sdk.logging"].logger
