# -*- coding: utf-8 -*-
"""不依赖 AstrBot 运行时的逻辑测试：对 astrbot.* 做最小桩替换后加载 main.py。"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import logging
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 最小桩
# ---------------------------------------------------------------------------

class FakeSP:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    async def session_get(self, umo, key, default=None):
        return copy.deepcopy(self.data.get((umo, key), default))

    async def session_put(self, umo, key, value):
        self.data[(umo, key)] = copy.deepcopy(value)

    async def remove_async(self, scope, scope_id, key):
        self.data.pop((scope_id, key), None)


class FakeStar:
    def __init__(self, context, config=None):
        self.context = context
        self.logger = logging.getLogger("test")


class FakeStarTools:
    _dir = tempfile.mkdtemp(prefix="gpersona-test-")

    @classmethod
    def get_data_dir(cls, name):
        p = Path(cls._dir) / name
        p.mkdir(parents=True, exist_ok=True)
        return p


class _CmdGroup:
    def command(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco


class FakeFilter:
    class EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"

    class PermissionType:
        ADMIN = "admin"

    def command_group(self, *args, **kwargs):
        def deco(fn):
            return _CmdGroup()

        return deco

    def command(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def permission_type(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco

    def on_waiting_llm_request(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco


def install_stubs() -> FakeSP:
    sp = FakeSP()

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("astrbot")
    api.sp = sp

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = FakeFilter()
    event.MessageChain = object

    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = FakeStar
    star.StarTools = FakeStarTools

    pkg = types.ModuleType("astrbot")
    pkg.api = api

    sys.modules["astrbot"] = pkg
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star
    return sp


sp = install_stubs()
spec = importlib.util.spec_from_file_location("gpersona_main", ROOT / "main.py")
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


class PersonaManager:
    personas_v3 = [{"name": "default"}, {"name": "客服小助手"}, {"name": "猫娘"}]

    def get_persona_v3_by_id(self, persona_id):
        return next(
            (p for p in self.personas_v3 if p["name"] == persona_id), None
        )


class Context:
    def __init__(self):
        self.persona_manager = PersonaManager()


class Event:
    def __init__(self, umo, platform_id="aiocqhttp", group_id="123456"):
        self.unified_msg_origin = umo
        self.platform_id = platform_id
        self.group_id = group_id

    def get_platform_id(self):
        return self.platform_id

    def get_group_id(self):
        return self.group_id


def make_plugin():
    sp.data.clear()
    data_file = FakeStarTools.get_data_dir(mod.PLUGIN_NAME) / mod.DATA_FILE_NAME
    if data_file.exists():
        data_file.unlink()
    plugin = mod.GroupPersonaPlugin(Context(), {"enabled": True})
    run(plugin._ensure_loaded())
    return plugin


def run(coro):
    return asyncio.run(coro)


def test_apply_and_preserve_other_fields():
    plugin = make_plugin()
    umo = "aiocqhttp:GroupMessage:789_123456"
    sp.data[(umo, mod.SESSION_CONFIG_KEY)] = {"llm_enabled": True, "persona_id": None}

    run(plugin._apply_to_umo(umo, "aiocqhttp:123456", "客服小助手"))

    saved = sp.data[(umo, mod.SESSION_CONFIG_KEY)]
    assert saved["persona_id"] == "客服小助手"
    assert saved["llm_enabled"] is True
    assert plugin.managed[umo]["prev"] is None
    assert plugin.managed[umo]["persona"] == "客服小助手"


def test_rebind_keeps_original_previous_value():
    plugin = make_plugin()
    umo = "aiocqhttp:GroupMessage:789_123456"
    sp.data[(umo, mod.SESSION_CONFIG_KEY)] = {"persona_id": "default"}

    run(plugin._apply_to_umo(umo, "aiocqhttp:123456", "客服小助手"))
    run(plugin._apply_to_umo(umo, "aiocqhttp:123456", "猫娘"))

    assert sp.data[(umo, mod.SESSION_CONFIG_KEY)]["persona_id"] == "猫娘"
    assert plugin.managed[umo]["persona"] == "猫娘"
    assert plugin.managed[umo]["prev"] == "default"


def test_external_change_becomes_new_previous_value():
    plugin = make_plugin()
    umo = "aiocqhttp:GroupMessage:789_123456"
    sp.data[(umo, mod.SESSION_CONFIG_KEY)] = {"persona_id": "default"}

    run(plugin._apply_to_umo(umo, "aiocqhttp:123456", "客服小助手"))
    # 模拟别人通过 WebUI 自定义规则改成「猫娘」
    sp.data[(umo, mod.SESSION_CONFIG_KEY)]["persona_id"] = "猫娘"
    run(plugin._apply_to_umo(umo, "aiocqhttp:123456", "客服小助手"))

    assert sp.data[(umo, mod.SESSION_CONFIG_KEY)]["persona_id"] == "客服小助手"
    assert plugin.managed[umo]["prev"] == "猫娘"


def test_unbind_restores_previous_value():
    plugin = make_plugin()
    key = "aiocqhttp:123456"
    umo = "aiocqhttp:GroupMessage:789_123456"
    sp.data[(umo, mod.SESSION_CONFIG_KEY)] = {"persona_id": "default"}
    plugin.bindings[key] = "客服小助手"

    run(plugin._apply_to_umo(umo, key, "客服小助手"))
    run(plugin._unbind_group(key))

    assert sp.data[(umo, mod.SESSION_CONFIG_KEY)]["persona_id"] == "default"
    assert umo not in plugin.managed
    assert key not in plugin.bindings


def test_unbind_without_previous_removes_persona_key_only():
    plugin = make_plugin()
    key = "aiocqhttp:123456"
    umo = "aiocqhttp:GroupMessage:789_123456"
    sp.data[(umo, mod.SESSION_CONFIG_KEY)] = {"llm_enabled": False, "persona_id": None}
    plugin.bindings[key] = "客服小助手"

    run(plugin._apply_to_umo(umo, key, "客服小助手"))
    run(plugin._unbind_group(key))

    saved = sp.data[(umo, mod.SESSION_CONFIG_KEY)]
    assert "persona_id" not in saved
    assert saved["llm_enabled"] is False


def test_two_members_under_isolation_share_group_persona():
    plugin = make_plugin()
    key = "aiocqhttp:123456"
    plugin.bindings[key] = "客服小助手"

    run(plugin._apply_to_umo("aiocqhttp:GroupMessage:111_123456", key, "客服小助手"))
    run(plugin._apply_to_umo("aiocqhttp:GroupMessage:222_123456", key, "客服小助手"))

    assert sp.data[("aiocqhttp:GroupMessage:111_123456", mod.SESSION_CONFIG_KEY)][
        "persona_id"
    ] == "客服小助手"
    assert sp.data[("aiocqhttp:GroupMessage:222_123456", mod.SESSION_CONFIG_KEY)][
        "persona_id"
    ] == "客服小助手"
    assert len(plugin.managed) == 2


def test_terminate_restores_everything():
    plugin = make_plugin()
    key = "aiocqhttp:123456"
    plugin.bindings[key] = "客服小助手"
    umo = "aiocqhttp:GroupMessage:789_123456"
    sp.data[(umo, mod.SESSION_CONFIG_KEY)] = {"persona_id": "default"}

    run(plugin._apply_to_umo(umo, key, "客服小助手"))
    run(plugin.terminate())

    assert sp.data[(umo, mod.SESSION_CONFIG_KEY)]["persona_id"] == "default"
    assert plugin.managed == {}


def test_hook_writes_only_for_bound_groups():
    plugin = make_plugin()
    key = "aiocqhttp:123456"
    plugin.bindings[key] = "客服小助手"
    umo = "aiocqhttp:GroupMessage:789_123456"

    run(plugin.enforce_group_persona(Event(umo)))
    assert sp.data[(umo, mod.SESSION_CONFIG_KEY)]["persona_id"] == "客服小助手"

    # 另一个群没有绑定，不应被写入
    other = "aiocqhttp:GroupMessage:789_999999"
    run(plugin.enforce_group_persona(Event(other, group_id="999999")))
    assert (other, mod.SESSION_CONFIG_KEY) not in sp.data

    # 私聊不处理
    private = "aiocqhttp:FriendMessage:789"
    run(plugin.enforce_group_persona(Event(private, group_id="")))
    assert (private, mod.SESSION_CONFIG_KEY) not in sp.data


def test_hook_skips_missing_persona():
    plugin = make_plugin()
    key = "aiocqhttp:123456"
    plugin.bindings[key] = "已经被删掉的人格"
    umo = "aiocqhttp:GroupMessage:789_123456"

    run(plugin.enforce_group_persona(Event(umo)))
    assert (umo, mod.SESSION_CONFIG_KEY) not in sp.data


def test_initialize_restores_orphaned_bindings():
    plugin = make_plugin()
    umo = "aiocqhttp:GroupMessage:789_123456"
    key = "aiocqhttp:123456"
    plugin.bindings[key] = "客服小助手"
    sp.data[(umo, mod.SESSION_CONFIG_KEY)] = {"persona_id": "default"}
    run(plugin._apply_to_umo(umo, key, "客服小助手"))

    # 模拟绑定在外部被删掉，然后插件重启
    plugin.bindings.clear()
    run(plugin._save())
    plugin2 = mod.GroupPersonaPlugin(Context(), {"enabled": True})
    run(plugin2.initialize())

    assert sp.data[(umo, mod.SESSION_CONFIG_KEY)]["persona_id"] == "default"
    assert plugin2.managed == {}


def test_reload_from_disk_keeps_state():
    plugin = make_plugin()
    key = "aiocqhttp:123456"
    plugin.bindings[key] = "客服小助手"
    umo = "aiocqhttp:GroupMessage:789_123456"
    sp.data[(umo, mod.SESSION_CONFIG_KEY)] = {"persona_id": "default"}
    run(plugin._apply_to_umo(umo, key, "客服小助手"))

    plugin2 = mod.GroupPersonaPlugin(Context(), {"enabled": True})
    run(plugin2._ensure_loaded())

    assert plugin2.bindings.get(key) == "客服小助手"
    assert plugin2.managed[umo]["prev"] == "default"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                raise
    if failures:
        raise SystemExit(1)
    print("all tests passed")
