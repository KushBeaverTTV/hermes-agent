"""Regression tests for #30170.

#30170: Sending a message while ``delegate_task`` is running killed the
subagent because the gateway always called ``running_agent.interrupt()``
on the parent, which then cascaded synchronously through
``AIAgent._active_children`` and aborted every in-flight subagent. The
reporter (and the linked Phase-1 spec) asked for the gateway to demote
``busy_input_mode='interrupt'`` to ``queue`` semantics whenever the
parent is currently driving subagents, while leaving explicit ``/stop``
and ``/new`` slash commands untouched.

These tests pin down the gateway-side guard introduced for #30170:

* ``GatewayRunner._agent_has_active_subagents`` correctly recognises
  parents that own real children, without false-positives from a
  ``MagicMock()._active_children`` auto-attribute, missing locks, or
  the ``_AGENT_PENDING_SENTINEL`` placeholder.
* ``_handle_active_session_busy_message`` demotes the interrupt mode to
  queue semantics (no ``interrupt()`` call, message merged into the
  pending queue, ack reflects the demotion) when the parent has active
  subagents.
* The ``queue`` and ``steer`` configured modes still behave exactly as
  before — the guard is interrupt-only.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ──────────────────────────────────────────────────────────────────────
# Minimal stubs so gateway imports cleanly (mirrors test_busy_session_ack)
# ──────────────────────────────────────────────────────────────────────
_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.config import (  # noqa: E402
    GatewayConfig,
    Platform,
    PlatformConfig,
    load_gateway_config,
)
from gateway.platforms.base import (  # noqa: E402
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Builders (parallel to tests/gateway/test_busy_session_ack.py)
# ──────────────────────────────────────────────────────────────────────
def _make_event(text: str = "hello", chat_id: str = "123") -> MessageEvent:
    source = SessionSource(
        platform=MagicMock(value="telegram"),
        chat_id=chat_id,
        chat_type="private",
        user_id="user1",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
    )


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner.adapters = {}
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, extra={})}
    )
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    # Exercise the production authorization path by default: ordinary test
    # users are paired/authorized, but pairing alone never grants exact-owner
    # control-plane authority.
    runner.pairing_store.is_approved.return_value = True
    return runner


def _set_owner_ids(runner, *user_ids: str) -> None:
    runner.config.platforms[Platform.TELEGRAM].extra["owner_user_ids"] = list(user_ids)


def _make_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value="telegram")
    return adapter


def _make_parent_with_subagents(
    *, children: int = 1, with_lock: bool = True
) -> MagicMock:
    """A MagicMock shaped like an AIAgent that currently owns *children* subagents."""
    parent = MagicMock()
    parent._active_children = [MagicMock() for _ in range(children)]
    parent._active_children_lock = threading.Lock() if with_lock else None
    parent.get_activity_summary.return_value = {
        "api_call_count": 7,
        "max_iterations": 60,
        "current_tool": "delegate_task",
    }
    return parent


def _make_parent_no_subagents() -> MagicMock:
    """A MagicMock shaped like an AIAgent that is NOT delegating."""
    parent = MagicMock()
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent.get_activity_summary.return_value = {
        "api_call_count": 3,
        "max_iterations": 60,
        "current_tool": "terminal",
    }
    return parent


# ──────────────────────────────────────────────────────────────────────
# _agent_has_active_subagents
# ──────────────────────────────────────────────────────────────────────
class TestAgentHasActiveSubagents:
    """The detection helper must be both precise and defensive."""

    def test_returns_false_for_none(self) -> None:
        assert GatewayRunner._agent_has_active_subagents(None) is False

    def test_returns_false_for_pending_sentinel(self) -> None:
        assert (
            GatewayRunner._agent_has_active_subagents(_AGENT_PENDING_SENTINEL)
            is False
        )

    def test_returns_false_when_attribute_missing(self) -> None:
        """Production AIAgents always have _active_children, but the helper
        must not blow up on test stubs or partial mocks."""

        class StubAgent:
            pass

        assert GatewayRunner._agent_has_active_subagents(StubAgent()) is False

    def test_returns_false_for_empty_list(self) -> None:
        assert (
            GatewayRunner._agent_has_active_subagents(_make_parent_no_subagents())
            is False
        )

    def test_returns_true_for_single_child(self) -> None:
        assert (
            GatewayRunner._agent_has_active_subagents(_make_parent_with_subagents())
            is True
        )

    def test_returns_true_for_many_children(self) -> None:
        assert (
            GatewayRunner._agent_has_active_subagents(
                _make_parent_with_subagents(children=5)
            )
            is True
        )

    def test_works_without_lock(self) -> None:
        """``_active_children_lock`` is optional in test stubs."""
        assert (
            GatewayRunner._agent_has_active_subagents(
                _make_parent_with_subagents(with_lock=False)
            )
            is True
        )

    def test_rejects_truthy_non_collection_attribute(self) -> None:
        """The MagicMock auto-attribute regression. ``MagicMock()._active_children``
        is itself a truthy MagicMock — without the isinstance guard, the
        helper would falsely report subagents on every test mock."""
        parent = MagicMock()  # no explicit _active_children setup
        assert GatewayRunner._agent_has_active_subagents(parent) is False

    @pytest.mark.parametrize(
        "container",
        [(MagicMock(),), {MagicMock()}, [MagicMock()]],
        ids=["tuple", "set", "list"],
    )
    def test_accepts_list_tuple_set(self, container: Any) -> None:
        parent = MagicMock()
        parent._active_children = container
        parent._active_children_lock = threading.Lock()
        assert GatewayRunner._agent_has_active_subagents(parent) is True


# ──────────────────────────────────────────────────────────────────────
# _handle_active_session_busy_message — interrupt demotion
# ──────────────────────────────────────────────────────────────────────
class TestBusyHandlerDemotesInterruptForSubagents:
    """The Phase-1 fix from #30170: parent.interrupt() must NOT fire when
    the parent is currently driving subagents."""

    @pytest.mark.asyncio
    async def test_does_not_call_interrupt_when_subagents_active(self) -> None:
        runner = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="follow up while subagent runs")
        sk = build_session_key(event.source)
        parent = _make_parent_with_subagents()
        runner._running_agents[sk] = parent
        runner.adapters[event.source.platform] = adapter

        handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        parent.interrupt.assert_not_called()
        # Message must still be queued so it gets picked up on the next turn
        # (stored via the FIFO path — its own turn, no destructive merge).
        assert adapter._pending_messages.get(sk) is event

    @pytest.mark.asyncio
    async def test_ack_explains_the_demotion(self) -> None:
        """The user-visible ack must mention the subagent context AND
        the `/stop` escape hatch so the operator can self-correct."""
        runner = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="hi mid-delegation")
        sk = build_session_key(event.source)
        parent = _make_parent_with_subagents()
        runner._running_agents[sk] = parent
        runner._running_agents_ts[sk] = time.time() - 120
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            await runner._handle_active_session_busy_message(event, sk)

        adapter._send_with_retry.assert_called_once()
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Subagent working" in content
        assert "queued" in content.lower()
        assert "/stop" in content
        assert "Interrupting" not in content

    @pytest.mark.asyncio
    async def test_interrupt_still_fires_when_no_subagents(self) -> None:
        """Regression-guard the other direction: with no subagents the
        demotion must NOT trigger and behaviour must be byte-identical
        to the pre-#30170 interrupt path."""
        runner = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="please stop")
        sk = build_session_key(event.source)
        parent = _make_parent_no_subagents()
        runner._running_agents[sk] = parent
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            await runner._handle_active_session_busy_message(event, sk)

        parent.interrupt.assert_called_once_with("please stop")
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Interrupting" in content
        assert "Subagent" not in content

    @pytest.mark.asyncio
    async def test_queue_mode_unchanged_with_subagents(self) -> None:
        """Configured ``queue`` mode is already subagent-safe; the new
        guard must not change its behaviour or its ack text."""
        runner = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()
        event = _make_event(text="queued during delegate")
        sk = build_session_key(event.source)
        parent = _make_parent_with_subagents()
        runner._running_agents[sk] = parent
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            await runner._handle_active_session_busy_message(event, sk)

        parent.interrupt.assert_not_called()
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        # The vanilla queue copy — NOT the #30170 "Subagent working" copy,
        # because the user explicitly asked for queue mode.
        assert "Queued for the next turn" in content
        assert "respond once the current task finishes" in content
        assert "Subagent working" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_still_routes_through_running_agent_steer(
        self,
    ) -> None:
        """Configured ``steer`` mode must reach ``running_agent.steer()``
        even when subagents are active — the #30170 demotion is
        interrupt-specific so it doesn't accidentally disable steer."""
        runner = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()
        event = _make_event(text="course-correct")
        sk = build_session_key(event.source)
        parent = _make_parent_with_subagents()
        parent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = parent
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            await runner._handle_active_session_busy_message(event, sk)

        parent.steer.assert_called_once_with("course-correct")
        parent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_exact_allowlist_owner_supersedes_steer_and_subagent_demotion(
        self, monkeypatch
    ) -> None:
        """Authenticated owner text is the next real turn, never a stale steer."""
        runner = _make_runner()
        _set_owner_ids(runner, "user1")
        runner._busy_input_mode = "steer"
        runner._busy_text_mode = "queue"
        adapter = _make_adapter()
        event = _make_event(text="stop stale work and answer this")
        sk = build_session_key(event.source)
        parent = _make_parent_with_subagents()
        parent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = parent
        runner.adapters[event.source.platform] = adapter

        handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        parent.steer.assert_not_called()
        parent.redirect.assert_not_called()
        parent.interrupt.assert_called_once_with(event.text)
        assert adapter._pending_messages.get(sk) is event

    @pytest.mark.asyncio
    async def test_exact_owner_preempts_full_media_queue_as_standalone_head(
        self, monkeypatch
    ) -> None:
        runner = _make_runner()
        _set_owner_ids(runner, "user1")
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()
        owner_event = _make_event(text="answer this now")
        sk = build_session_key(owner_event.source)
        stale_media = _make_event(text="stale media")
        stale_media.message_type = MessageType.PHOTO
        stale_media.media_urls = ["https://example.invalid/stale.jpg"]
        stale_overflow = [_make_event(text=f"stale-{index}") for index in range(31)]
        adapter._pending_messages[sk] = stale_media
        runner._queued_events = {sk: list(stale_overflow)}
        parent = _make_parent_with_subagents()
        runner._running_agents[sk] = parent
        runner.adapters[owner_event.source.platform] = adapter

        handled = await runner._handle_active_session_busy_message(owner_event, sk)

        assert handled is True
        assert adapter._pending_messages[sk] is owner_event
        assert runner._queued_events[sk][0] is stale_media
        assert runner._queued_events[sk][1:] == stale_overflow[:30]
        assert len(runner._queued_events[sk]) == 31
        assert stale_overflow[-1] not in runner._queued_events[sk]
        parent.interrupt.assert_called_once_with(owner_event.text)

    @pytest.mark.asyncio
    async def test_adapter_owner_falls_back_to_queue_when_preemption_rejects(
        self,
    ) -> None:
        runner = _make_runner()
        _set_owner_ids(runner, "user1")
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="owner survives adapter rejection")
        sk = build_session_key(event.source)
        parent = _make_parent_no_subagents()
        runner._running_agents[sk] = parent
        runner.adapters[event.source.platform] = adapter
        runner._preempt_busy_queue_with_owner_event = MagicMock(return_value=False)

        handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        assert adapter._pending_messages[sk] is event
        parent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_adapter_drain_owner_falls_back_when_preemption_rejects(
        self,
    ) -> None:
        runner = _make_runner()
        _set_owner_ids(runner, "user1")
        runner._draining = True
        runner._busy_input_mode = "queue"
        runner._restart_requested = True
        runner._status_action_gerund = lambda: "restarting"
        runner._reply_anchor_for_event = lambda event: None
        runner._thread_metadata_for_source = lambda *_args, **_kwargs: None
        adapter = _make_adapter()
        event = _make_event(text="owner survives adapter drain")
        sk = build_session_key(event.source)
        runner._running_agents[sk] = _AGENT_PENDING_SENTINEL
        runner.adapters[event.source.platform] = adapter
        runner._preempt_busy_queue_with_owner_event = MagicMock(return_value=False)

        handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        assert adapter._pending_messages[sk] is event
        adapter._send_with_retry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_allow_all_guest_remains_queued_behind_active_subagent(
        self, monkeypatch
    ) -> None:
        """Broad chat permission is not owner control-plane authority."""
        monkeypatch.setattr(
            "gateway.authz_mixin._auth_env",
            lambda name, default="": "true"
            if name == "DISCORD_ALLOW_ALL_USERS"
            else default,
        )
        runner = _make_runner()
        # Force this assertion through DISCORD_ALLOW_ALL_USERS rather than the
        # pairing fallback.
        runner.pairing_store.is_approved = MagicMock(return_value=False)
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        adapter.config = PlatformConfig(enabled=True, extra={})
        event = _make_event(text="guest follow-up")
        event.source.platform = Platform.DISCORD
        event.source.user_id = "guest"
        event.source.user_name = "Kush Beaver"
        sk = build_session_key(event.source)
        parent = _make_parent_with_subagents()
        runner._running_agents[sk] = parent
        runner.config.platforms[Platform.DISCORD] = adapter.config
        runner.adapters[Platform.DISCORD] = adapter

        assert runner._is_user_authorized(event.source) is True
        assert runner._is_explicit_owner_source(event.source) is False

        handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        parent.interrupt.assert_not_called()
        assert adapter._pending_messages.get(sk) is event

    @pytest.mark.asyncio
    async def test_approved_paired_guest_is_authorized_but_cannot_preempt_owner_turn(
        self,
    ) -> None:
        runner = _make_runner()
        _set_owner_ids(runner, "actual-owner")
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="paired guest follow-up")
        event.source.user_id = "paired-guest"
        sk = build_session_key(event.source)
        parent = _make_parent_with_subagents()
        runner._running_agents[sk] = parent
        runner.adapters[event.source.platform] = adapter

        assert runner._is_user_authorized(event.source) is True
        assert runner._is_explicit_owner_source(event.source) is False

        handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        parent.interrupt.assert_not_called()
        assert adapter._pending_messages.get(sk) is event

    @pytest.mark.asyncio
    async def test_pending_sentinel_does_not_demote(self) -> None:
        """The placeholder ``_AGENT_PENDING_SENTINEL`` is not a real
        agent — the guard must not treat it as having subagents.
        Otherwise we'd permanently queue messages for sessions that
        haven't actually started running yet."""
        runner = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="follow up before start")
        sk = build_session_key(event.source)
        runner._running_agents[sk] = _AGENT_PENDING_SENTINEL
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        # Sentinel can't be interrupted (no .interrupt to call) — verify
        # that the helper still returns the "interrupting" copy because
        # demotion did NOT fire (and the sentinel branch in the real
        # handler just skips the interrupt call silently).
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Subagent working" not in content


class TestExplicitOwnerSource:
    def test_secondary_profile_never_inherits_default_owner_ids(self) -> None:
        runner = _make_runner()
        _set_owner_ids(runner, "default-owner")
        secondary = _make_adapter()
        secondary.config = PlatformConfig(enabled=True, extra={})
        runner._profile_adapters = {
            "secondary": {Platform.TELEGRAM: secondary}
        }
        source = _make_event().source
        source.platform = Platform.TELEGRAM
        source.profile = "secondary"
        source.user_id = "default-owner"

        assert runner._is_explicit_owner_source(source) is False

        secondary.config.extra["owner_user_ids"] = ["secondary-owner"]
        source.user_id = "secondary-owner"
        assert runner._is_explicit_owner_source(source) is True

    def test_real_yaml_path_bridges_owner_ids_into_platform_extra(
        self, monkeypatch, tmp_path
    ) -> None:
        (tmp_path / "config.yaml").write_text(
            "gateway:\n"
            "  platforms:\n"
            "    telegram:\n"
            "      enabled: true\n"
            "      owner_user_ids:\n"
            "        - config-owner\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("gateway.config.get_hermes_home", lambda: tmp_path)

        config = load_gateway_config()

        assert config.platforms[Platform.TELEGRAM].extra["owner_user_ids"] == [
            "config-owner"
        ]

    def test_loads_owner_ids_from_platform_config_and_ignores_env(self, monkeypatch) -> None:
        monkeypatch.setenv("TELEGRAM_OWNER_USER_IDS", "env-only")
        runner = _make_runner()
        runner.config = GatewayConfig.from_dict(
            {
                "platforms": {
                    "telegram": {
                        "enabled": True,
                        "extra": {"owner_user_ids": ["config-owner"]},
                    }
                }
            }
        )
        source = _make_event().source
        source.platform = Platform.TELEGRAM

        source.user_id = "config-owner"
        assert runner._is_explicit_owner_source(source) is True

        source.user_id = "env-only"
        assert runner._is_explicit_owner_source(source) is False

    def test_matches_exact_primary_user_id_but_not_alt_only(self, monkeypatch) -> None:
        runner = _make_runner()
        _set_owner_ids(runner, "owner-primary", "owner-alt")
        source = _make_event().source

        source.user_id = "owner-primary"
        assert runner._is_explicit_owner_source(source) is True

        source.user_id = "other"
        source.user_id_alt = "owner-alt"
        assert runner._is_explicit_owner_source(source) is False

    def test_rejects_wildcard_and_user_controlled_labels(self, monkeypatch) -> None:
        runner = _make_runner()
        _set_owner_ids(runner, "*")
        source = _make_event().source
        source.user_id = "guest"
        source.user_name = "owner-primary"
        source.chat_name = "Owner room"

        assert runner._is_explicit_owner_source(source) is False
