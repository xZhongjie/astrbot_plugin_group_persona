# -*- coding: utf-8 -*-
"""astrbot_plugin_group_persona — 给指定群聊绑定专属人格。

核心思路：
* AstrBot 的人格解析以「会话（UMO）」为单位，优先级从高到低大致是：
  会话强制人格（WebUI 的「自定义规则 → 人格配置」）> 对话级人格（/persona）>
  配置文件默认人格。
* 本插件在「即将调用 LLM」的钩子里，把当前群对应会话的
  ``session_service_config.persona_id`` 写成绑定的目标人格——也就是复用了
  AstrBot 自己「强制人格」的最高优先级通道，因此能压过系统默认人格、
  /persona 以及已有的自定义规则。
* 写入按 UMO 进行：没开「隔离对话」时一个群就是一个 UMO；开了隔离后
  每个成员是独立 UMO，插件会在各自第一次请求时分别写入，效果仍然是
  “整个群都用同一个人格”。

设计取舍：
* 只在真正要调用 LLM 时才写（OnWaitingLLMRequestEvent），不注册
  AdapterMessageEvent 监听器，避免把群里每条消息都当成唤醒。
* 写入前记录被覆盖的旧值，解绑/卸载/重载时尽量恢复，避免留下“幽灵规则”。
* 只改动 ``persona_id`` 字段，其余会话配置（llm_enabled 等）原样保留。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_group_persona"
SESSION_CONFIG_KEY = "session_service_config"
DATA_FILE_NAME = "bindings.json"
DATA_VERSION = 1


def _group_key(event: AstrMessageEvent) -> str | None:
    """返回 ``平台实例ID:群号``，私聊或拿不到群号时返回 None。"""
    group_id = (event.get_group_id() or "").strip()
    if not group_id:
        return None
    return f"{event.get_platform_id()}:{group_id}"


@filter.command_group("gpersona", alias={"群人格"})
def gpersona():
    """群专属人格：/gpersona bind|unbind|status|list"""


class GroupPersonaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.conf = config or {}
        self.bindings: dict[str, str] = {}
        """``平台实例ID:群号`` -> 人格 ID。"""
        self.managed: dict[str, dict[str, Any]] = {}
        """被本插件改写过人格的 UMO -> {"group": 群 key, "persona": 人格, "prev": 原值}"""
        self._lock = asyncio.Lock()
        self._data_dir: Path | None = None
        self._loaded = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        await self._ensure_loaded()
        # 启动时对账：绑定已经被删掉（比如在别的机器上解绑）的会话，恢复原值。
        changed = False
        async with self._lock:
            for umo, entry in list(self.managed.items()):
                if entry.get("group") not in self.bindings:
                    await self._restore_managed_locked(umo)
                    self.managed.pop(umo, None)
                    changed = True
        if changed:
            await self._save()

    async def terminate(self) -> None:
        """插件卸载/重载/进程正常退出时，尽量把被覆盖的人格还回去。"""
        if not self.conf.get("restore_on_unload", True):
            return
        await self._ensure_loaded()
        async with self._lock:
            for umo in list(self.managed.keys()):
                try:
                    await self._restore_managed_locked(umo)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("恢复会话 %s 的人格失败：%s", umo, exc)
                self.managed.pop(umo, None)
            await self._save()

    # ------------------------------------------------------------------
    # 核心：写入强制人格
    # ------------------------------------------------------------------

    @filter.on_waiting_llm_request()
    async def enforce_group_persona(self, event: AstrMessageEvent) -> None:
        """在真正构建 LLM 请求前，把本群会话的人格改成绑定值。

        该钩子在 AstrBot 本地 Agent Runner 中位于 ``build_main_agent`` 之前，
        因此随后的人格解析会直接读到我们写入的「强制人格」。
        """
        try:
            await self._ensure_loaded()
            if not self.conf.get("enabled", True):
                return
            key = _group_key(event)
            if not key:
                return
            persona_id = (self.bindings.get(key) or "").strip()
            if not persona_id:
                return
            if not self._persona_exists(persona_id):
                self.logger.warning(
                    "群 %s 绑定的人格 %r 不存在，本次跳过强制人格。",
                    key,
                    persona_id,
                )
                return

            await self._apply_to_umo(
                umo=event.unified_msg_origin,
                group_key=key,
                persona_id=persona_id,
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("写入群专属人格失败")

    async def _apply_to_umo(self, umo: str, group_key: str, persona_id: str) -> None:
        async with self._lock:
            current = await sp.session_get(umo, SESSION_CONFIG_KEY, {})
            if not isinstance(current, dict):
                current = {}

            if current.get("persona_id") == persona_id:
                # 已是目标人格：如果是我们之前写的，确保归属信息最新即可。
                entry = self.managed.get(umo)
                if entry is not None and entry.get("group") != group_key:
                    entry["group"] = group_key
                    await self._save()
                return

            entry = self.managed.get(umo)
            if entry is None:
                # 第一次接管：记下原本的人格（可能没有规则）。
                prev = current.get("persona_id")
            elif current.get("persona_id") == entry.get("persona"):
                # 是我们上一次写入的值，只是绑定换了一个人格：保留最初的原值。
                prev = entry.get("prev")
            else:
                # 被外部改过（例如有人编辑了自定义规则），以最新值为准。
                prev = current.get("persona_id")

            updated = dict(current)
            updated["persona_id"] = persona_id
            await sp.session_put(umo, SESSION_CONFIG_KEY, updated)

            self.managed[umo] = {
                "group": group_key,
                "persona": persona_id,
                "prev": prev,
            }
            await self._save()

            if self.conf.get("log_applied", False):
                self.logger.info(
                    "已将会话 %s 强制为人格 %s（原值 %r）。",
                    umo,
                    persona_id,
                    prev,
                )

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @gpersona.command("bind", alias={"绑定"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpersona_bind(self, event: AstrMessageEvent, persona_id: str = ""):
        """把当前群绑定到指定人格（需要管理员）。"""
        key = _group_key(event)
        if not key:
            yield event.plain_result("这条指令只能在群聊里使用。")
            return
        if not self.conf.get("enabled", True):
            yield event.plain_result("插件已在配置中关闭，请先在 WebUI 插件配置里打开。")
            return

        pid = (persona_id or "").strip()
        if not pid:
            yield event.plain_result(
                "用法：/gpersona bind <人格ID>\n\n" + self._persona_list_text()
            )
            return

        if not self._persona_exists(pid):
            yield event.plain_result(
                f"没有找到人格「{pid}」。\n\n" + self._persona_list_text()
            )
            return

        await self._ensure_loaded()
        async with self._lock:
            self.bindings[key] = pid
            await self._save()

        yield event.plain_result(
            f"已把本群绑定到人格「{pid}」。\n"
            "之后本群的所有对话（包括新建对话；开启「隔离对话」后也包括每个成员）"
            "都会强制使用该人格，优先级高于默认人格、/persona 和已有的自定义规则。\n"
            "取消绑定：/gpersona unbind"
        )

    @gpersona.command("unbind", alias={"解绑"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpersona_unbind(self, event: AstrMessageEvent):
        """取消当前群的绑定，并恢复被覆盖的原人格规则。"""
        key = _group_key(event)
        if not key:
            yield event.plain_result("这条指令只能在群聊里使用。")
            return

        await self._ensure_loaded()
        existed, restored = await self._unbind_group(key)

        if not existed:
            yield event.plain_result("本群当前没有绑定人格。")
        else:
            extra = f"，已恢复 {restored} 个会话的原规则" if restored else ""
            yield event.plain_result(f"已取消本群的人格绑定{extra}。")

    @gpersona.command("status", alias={"状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpersona_status(self, event: AstrMessageEvent):
        """查看当前群的绑定状态。"""
        key = _group_key(event)
        if not key:
            yield event.plain_result("这条指令只能在群聊里使用。")
            return
        await self._ensure_loaded()
        persona_id = self.bindings.get(key)
        if not persona_id:
            yield event.plain_result("本群当前没有绑定专属人格。")
            return

        umo = event.unified_msg_origin
        current = await sp.session_get(umo, SESSION_CONFIG_KEY, {})
        current_pid = current.get("persona_id") if isinstance(current, dict) else None
        lines = [
            "本群专属人格",
            f"  群标识：{key}",
            f"  绑定人格：{persona_id}",
            f"  当前会话：{umo}",
            f"  当前已写入：{current_pid if current_pid is not None else '（无）'}",
        ]
        yield event.plain_result("\n".join(lines))

    @gpersona.command("list", alias={"列表"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpersona_list(self, event: AstrMessageEvent):
        """列出所有已绑定的群。"""
        await self._ensure_loaded()
        if not self.bindings:
            yield event.plain_result("还没有任何群绑定专属人格。")
            return
        lines = ["已绑定的群："]
        for key, pid in sorted(self.bindings.items()):
            lines.append(f"  {key} -> {pid}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 存储与恢复
    # ------------------------------------------------------------------

    async def _unbind_group(self, group_key: str) -> tuple[bool, int]:
        """取消一个群的绑定并恢复它名下所有会话，返回 (是否存在绑定, 恢复了几个会话)。"""
        async with self._lock:
            existed = self.bindings.pop(group_key, None)
            restored = 0
            for umo, entry in list(self.managed.items()):
                if entry.get("group") == group_key:
                    await self._restore_managed_locked(umo)
                    self.managed.pop(umo, None)
                    restored += 1
            await self._save()
            return existed is not None, restored

    async def _restore_managed_locked(self, umo: str) -> None:
        """把某个会话恢复成插件接管前的人格（调用方需持有 self._lock）。"""
        entry = self.managed.get(umo)
        if not entry:
            return
        current = await sp.session_get(umo, SESSION_CONFIG_KEY, {})
        if not isinstance(current, dict):
            current = {}
        if current.get("persona_id") != entry.get("persona"):
            # 外部已经改过，不要覆盖别人的新设置。
            return

        prev = entry.get("prev")
        if prev is None:
            current.pop("persona_id", None)
            if current:
                await sp.session_put(umo, SESSION_CONFIG_KEY, current)
            else:
                await sp.remove_async("umo", umo, SESSION_CONFIG_KEY)
        else:
            current["persona_id"] = prev
            await sp.session_put(umo, SESSION_CONFIG_KEY, current)

    def _persona_exists(self, persona_id: str) -> bool:
        """兼容不同 AstrBot 版本：直接查 personas_v3，不依赖较新的便捷方法。"""
        if not persona_id:
            return False
        if persona_id == "default":
            return True
        try:
            personas = self.context.persona_manager.personas_v3
        except Exception:  # noqa: BLE001
            return True
        return any(p.get("name") == persona_id for p in personas)

    def _persona_list_text(self) -> str:
        try:
            names = [
                str(p.get("name"))
                for p in self.context.persona_manager.personas_v3
                if p.get("name")
            ]
        except Exception:  # noqa: BLE001
            names = []
        if not names:
            return "可用人格：请在 WebUI「人格」页面查看。"
        shown = "、".join(names[:40])
        more = f" 等 {len(names)} 个" if len(names) > 40 else ""
        return f"可用人格：{shown}{more}"

    def _data_file(self) -> Path:
        if self._data_dir is None:
            self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        return self._data_dir / DATA_FILE_NAME

    def _ensure_loaded_sync(self) -> None:
        if self._loaded:
            return
        path = self._data_file()
        raw: dict[str, Any] = {}
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取群人格数据失败：%s", exc)

        bindings = raw.get("bindings") if isinstance(raw, dict) else None
        if isinstance(bindings, dict):
            self.bindings = {
                str(k): str(v)
                for k, v in bindings.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }

        managed = raw.get("managed") if isinstance(raw, dict) else None
        if isinstance(managed, dict):
            for umo, entry in managed.items():
                if not isinstance(umo, str) or not isinstance(entry, dict):
                    continue
                group = entry.get("group")
                persona = entry.get("persona")
                if not isinstance(group, str) or not isinstance(persona, str):
                    continue
                prev = entry.get("prev")
                if prev is not None and not isinstance(prev, str):
                    prev = None
                self.managed[umo] = {
                    "group": group,
                    "persona": persona,
                    "prev": prev,
                }
        self._loaded = True

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if not self._loaded:
                self._ensure_loaded_sync()

    async def _save(self) -> None:
        """原子写出绑定与接管记录（调用方通常已持有 self._lock）。"""
        path = self._data_file()
        payload = {
            "version": DATA_VERSION,
            "bindings": self.bindings,
            "managed": self.managed,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("保存群人格数据失败：%s", exc)
