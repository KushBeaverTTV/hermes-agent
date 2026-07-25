"""Regression test: the ``_handle_message`` PRIORITY busy-path must also
demote ``busy_input_mode='interrupt'`` to queue semantics when context
compression is in flight (#56391), the same as
``_handle_active_session_busy_message`` already does.

Both code paths handle a message arriving while an agent is already running
for the session. ``_handle_active_session_busy_message`` (the
``busy_session_handler`` callback most platform adapters register via
``gateway/platforms/base.py``) demotes ``interrupt`` -> ``queue`` for two
independent reasons:

  * active subagents (#30170)
  * context compression in flight (#56391)

``_handle_message`` has its own, independent inline "PRIORITY" busy-handling
block (see the ``if _quick_key in self._running_agents:`` guard) that a
plain-text follow-up reaches directly — mirrors_test_running_agent_session_
toggles.py already proves ``_handle_message`` is invoked directly with an
active running agent, not only through the adapter dispatch layer. That
PRIORITY block's own comment says it mirrors
``_handle_active_session_busy_message``'s subagent-demotion rationale
verbatim, and it does demote for active subagents — but it never checks
``_session_has_compression_in_flight``, so a plain-text follow-up landing on
this path while compression is mid-flight still interrupts, racing a new
turn against the pre-rotation parent session exactly as #56391 describes.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source(*, user_id: str = "u1", user_id_alt: str | None = None) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        user_id_alt=user_id_alt,
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(
    text: str,
    *,
    user_id: str = "u1",
    user_id_alt: str | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(user_id=user_id, user_id_alt=user_id_alt),
        message_id="m1",
    )


def _make_runner(*, compression_in_flight: bool):
    """Minimal GatewayRunner with an active running agent for this session.

    Mirrors tests/gateway/test_running_agent_session_toggles.py's harness
    (proven to drive _handle_message end-to-end with a live running agent),
    extended with the compression-lock plumbing
    _session_has_compression_in_flight reads.
    """
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    source = _make_source()
    sk = build_session_key(source)
    session_entry = SessionEntry(
        session_key=sk,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    session_store = MagicMock()
    session_store.get_or_create_session.return_value = session_entry
    session_store.load_transcript.return_value = []
    session_store.has_any_sessions.return_value = True
    session_store.append_to_transcript = MagicMock()
    session_store.rewrite_transcript = MagicMock()
    session_store.update_session = MagicMock()
    runner.session_store = session_store

    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._draining = False
    runner._busy_input_mode = "interrupt"

    # No subagents active — isolates the compression-demotion behavior from
    # the (already-correct) subagent-demotion branch.
    runner._agent_has_active_subagents = lambda _agent: False
    runner._session_has_compression_in_flight = AsyncMock(
        return_value=compression_in_flight
    )

    import time
    agent_mock = MagicMock()
    agent_mock.get_activity_summary.return_value = {
        "seconds_since_activity": 0.0,
        "last_activity_desc": "api_call",
        "api_call_count": 1,
        "max_iterations": 60,
    }
    runner._running_agents[sk] = agent_mock
    # Past the Telegram follow-up grace window (HERMES_TELEGRAM_FOLLOWUP_
    # GRACE_SECONDS, default 3.0s) so the message reaches the PRIORITY
    # interrupt/steer/subagent-demotion block instead of the earlier
    # "just started, queue without interrupt" grace-period branch.
    runner._running_agents_ts[sk] = time.time() - 120
    return runner, agent_mock, sk


@pytest.mark.asyncio
async def test_priority_path_does_not_interrupt_when_compression_in_flight():
    """A plain-text follow-up must NOT interrupt the running agent while
    context compression is in flight — it must queue instead, mirroring
    _handle_active_session_busy_message's #56391 demotion."""
    runner, agent_mock, sk = _make_runner(compression_in_flight=True)

    await runner._handle_message(_make_event("still there?"))

    agent_mock.interrupt.assert_not_called()
    queued = runner.adapters[Platform.TELEGRAM]._pending_messages.get(sk)
    assert queued is not None and queued.text == "still there?"


@pytest.mark.asyncio
async def test_priority_path_still_interrupts_without_compression_lock():
    """Sanity control: without a compression lock, the PRIORITY path's
    default interrupt behavior is unchanged."""
    runner, agent_mock, sk = _make_runner(compression_in_flight=False)

    await runner._handle_message(_make_event("still there?"))

    agent_mock.interrupt.assert_called_once_with("still there?")


@pytest.mark.asyncio
async def test_exact_owner_priority_path_bypasses_steer_and_queues_next_turn(
    monkeypatch,
):
    """Owner text is a new turn, never an injection into stale work."""
    monkeypatch.setattr(
        "gateway.authz_mixin._auth_env",
        lambda name, default="": "u1" if name == "TELEGRAM_ALLOWED_USERS" else default,
    )
    runner, agent_mock, sk = _make_runner(compression_in_flight=False)
    runner._busy_input_mode = "steer"
    agent_mock.steer.return_value = True

    await runner._handle_message(_make_event("new owner direction"))

    agent_mock.steer.assert_not_called()
    agent_mock.interrupt.assert_called_once_with("new owner direction")
    queued = runner.adapters[Platform.TELEGRAM]._pending_messages.get(sk)
    assert queued is not None and queued.text == "new owner direction"


@pytest.mark.asyncio
async def test_exact_owner_priority_path_bypasses_subagent_and_compression_demotion(
    monkeypatch,
):
    monkeypatch.setattr(
        "gateway.authz_mixin._auth_env",
        lambda name, default="": "u1" if name == "TELEGRAM_ALLOWED_USERS" else default,
    )
    runner, agent_mock, sk = _make_runner(compression_in_flight=True)
    runner._agent_has_active_subagents = lambda running_agent: True

    await runner._handle_message(_make_event("supersede the stale batch"))

    agent_mock.interrupt.assert_called_once_with("supersede the stale batch")
    queued = runner.adapters[Platform.TELEGRAM]._pending_messages.get(sk)
    assert queued is not None and queued.text == "supersede the stale batch"


@pytest.mark.asyncio
async def test_exact_owner_priority_path_bypasses_telegram_grace_and_queue_mode(
    monkeypatch,
):
    import time

    monkeypatch.setattr(
        "gateway.authz_mixin._auth_env",
        lambda name, default="": "u1" if name == "TELEGRAM_ALLOWED_USERS" else default,
    )
    runner, agent_mock, sk = _make_runner(compression_in_flight=False)
    runner._busy_input_mode = "queue"
    runner._running_agents_ts[sk] = time.time()

    await runner._handle_message(_make_event("owner now"))

    agent_mock.interrupt.assert_called_once_with("owner now")
    queued = runner.adapters[Platform.TELEGRAM]._pending_messages.get(sk)
    assert queued is not None and queued.text == "owner now"


@pytest.mark.asyncio
async def test_exact_owner_priority_path_preempts_existing_fifo_head(monkeypatch):
    monkeypatch.setattr(
        "gateway.authz_mixin._auth_env",
        lambda name, default="": "u1" if name == "TELEGRAM_ALLOWED_USERS" else default,
    )
    runner, agent_mock, sk = _make_runner(compression_in_flight=False)
    adapter = runner.adapters[Platform.TELEGRAM]
    stale_head = _make_event("stale head")
    stale_tail = _make_event("stale tail")
    adapter._pending_messages[sk] = stale_head
    runner._queued_events = {sk: [stale_tail]}

    owner_event = _make_event("owner is next")
    await runner._handle_message(owner_event)

    assert adapter._pending_messages[sk] is owner_event
    assert runner._queued_events[sk] == [stale_head, stale_tail]
    agent_mock.interrupt.assert_called_once_with(owner_event.text)


@pytest.mark.asyncio
async def test_exact_owner_preempts_full_media_queue_while_agent_is_starting(
    monkeypatch,
):
    from gateway.run import _AGENT_PENDING_SENTINEL

    monkeypatch.setattr(
        "gateway.authz_mixin._auth_env",
        lambda name, default="": "u1" if name == "TELEGRAM_ALLOWED_USERS" else default,
    )
    runner, agent_mock, sk = _make_runner(compression_in_flight=False)
    adapter = runner.adapters[Platform.TELEGRAM]
    stale_media = _make_event("stale media")
    stale_media.message_type = MessageType.PHOTO
    stale_media.media_urls = ["https://example.invalid/stale.jpg"]
    stale_overflow = [_make_event(f"stale-{index}") for index in range(31)]
    adapter._pending_messages[sk] = stale_media
    runner._queued_events = {sk: list(stale_overflow)}
    runner._running_agents[sk] = _AGENT_PENDING_SENTINEL

    owner_event = _make_event("owner is next")
    await runner._handle_message(owner_event)

    assert adapter._pending_messages[sk] is owner_event
    assert runner._queued_events[sk][0] is stale_media
    assert runner._queued_events[sk][1:] == stale_overflow[:30]
    assert len(runner._queued_events[sk]) == 31
    assert stale_overflow[-1] not in runner._queued_events[sk]
    agent_mock.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_internal_event_cannot_gain_priority_owner_authority(monkeypatch):
    monkeypatch.setattr(
        "gateway.authz_mixin._auth_env",
        lambda name, default="": "u1" if name == "TELEGRAM_ALLOWED_USERS" else default,
    )
    runner, agent_mock, _sk = _make_runner(compression_in_flight=False)
    runner._busy_input_mode = "steer"
    agent_mock.steer.return_value = True
    internal_event = _make_event("internal follow-up")
    internal_event.internal = True

    await runner._handle_message(internal_event)

    agent_mock.interrupt.assert_not_called()
    agent_mock.steer.assert_called_once_with(internal_event.text)


@pytest.mark.asyncio
async def test_alternate_id_only_cannot_gain_priority_owner_authority(monkeypatch):
    monkeypatch.setattr(
        "gateway.authz_mixin._auth_env",
        lambda name, default="": "owner-alt"
        if name == "TELEGRAM_ALLOWED_USERS"
        else default,
    )
    runner, agent_mock, _sk = _make_runner(compression_in_flight=False)
    runner._busy_input_mode = "steer"
    agent_mock.steer.return_value = True

    await runner._handle_message(
        _make_event("ordinary follow-up", user_id="paired-primary", user_id_alt="owner-alt")
    )

    agent_mock.interrupt.assert_not_called()
    agent_mock.steer.assert_called_once_with("ordinary follow-up")
