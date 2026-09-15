"""Reasoning-only clean stop must not be promoted to the answer when it is a stall.

Regression for #111761: a model that is handed tools, has not executed any yet this
turn, and returns reasoning with no content and no tool call is not answering — it is
stalling. Promoting the planning monologue invented a plausible-looking "I'm about to
do it" reply and ended the turn as a clean success, so a model that never emits a tool
call (observed: glm-5-3-flash-high over ACP, 4/4 turns, session tool_call_count=0) was
reported as healthy and the surface showed no error.

The predicate is imported lazily so this file still imports against an unfixed tree —
that way both tests fail on the behaviour, not on a missing symbol.
"""
from types import SimpleNamespace

import pytest

from agent.turn_final_response import finish_text_response


def _agent(tool_names):
    """Stub carrying only what the promotion branch and the observed seam touch."""
    return SimpleNamespace(
        valid_tool_names=tool_names,
        model="glm-5-3-flash-high",
        provider="devin",
        _extract_reasoning=lambda m: getattr(m, "reasoning", None),
        _mute_post_response=True,
        _has_content_after_think_block=lambda s: bool(s and s.strip()),
        _strip_think_blocks=lambda s: s or "",
    )


_PLANNING = "Let me batch the terminal calls and run them in parallel."
_ANSWER = "The disk has 41 GB free."


@pytest.mark.parametrize(
    "tool_names,api_call_count,reasoning,expected",
    [
        # The bug: tools offered, model has not acted yet, reasoning is planning -> stall.
        (["terminal", "read_file"], 1, _PLANNING, True),
        # Real glm-5-3-flash-high tails from the broken session must all stall.
        (["terminal"], 1, "...memory. Let me load the doctrine skill first, then run checks.", True),
        (["terminal"], 1, "...Let me do 3-4 parallel terminal calls.", True),
        # A genuine answer-in-reasoning is NOT a stall, even on the first call with
        # tools available — that is the documented contract upstream asserts in
        # test_reasoning_only_local_clean_stop_returns_immediately.
        (["terminal", "read_file"], 1, "reasoning only", False),
        (["terminal", "read_file"], 1, "structured reasoning answer", False),
        (["terminal", "read_file"], 1, _ANSWER, False),
        # Deep into a turn: the parser-compat case the promotion was written for
        # (vLLM nemotron_v3 past ~500K prompt tokens) must keep promoting.
        (["terminal", "read_file"], 2, _PLANNING, False),
        (["terminal", "read_file"], 40, _PLANNING, False),
        # No tools in the schema: a pure-chat answer-in-reasoning is still an answer.
        ([], 1, _PLANNING, False),
        (None, 1, _PLANNING, False),
    ],
)
def test_reasoning_only_is_stall(tool_names, api_call_count, reasoning, expected):
    from agent.turn_final_response import reasoning_only_is_stall

    assert reasoning_only_is_stall(_agent(tool_names), api_call_count, reasoning) is expected


def test_stall_hands_off_to_empty_response_recovery_instead_of_promoting(monkeypatch, caplog):
    """A reasoning-only stall leaves content empty and reaches recover_empty_response.

    Proves the wiring, not just the predicate: the monologue never becomes the visible
    reply, and the turn goes to the designed empty-response ladder instead of being
    reported as a completed answer.
    """
    import agent.turn_final_response as mod

    seen = {}

    class _Ev:
        action = "break"
        final_response = ""
        turn_exit_reason = "empty_recovery"
        active_system_prompt = None
        preflight_compression_blocked = False

    def fake_recover(agent, assistant_message, response, finish_reason, **kwargs):
        seen["called"] = True
        seen["final_response"] = kwargs.get("final_response")
        return _Ev()

    monkeypatch.setattr(mod, "recover_empty_response", fake_recover)

    msg = SimpleNamespace(
        content="",
        tool_calls=None,
        reasoning="Let me batch the terminal calls and run them in parallel.",
    )
    messages = [{"role": "user", "content": "do a systems check"}]

    with caplog.at_level("WARNING", logger="agent.conversation_loop"):
        verdict = finish_text_response(
            _agent(["terminal", "read_file"]),
            assistant_message=msg, response=None, finish_reason="stop",
            messages=messages, api_messages=list(messages),
            conversation_history=[], api_call_count=1,
            user_message="do a systems check", active_system_prompt=None,
            final_response="", _turn_exit_reason=None,
            _preflight_compression_blocked=False, codex_ack_continuations=0,
            truncated_response_parts=[], length_continue_retries=0,
            _pending_verification_response=None,
            _pending_verification_response_previewed=False,
        )

    # The monologue was NOT promoted to the visible answer.
    assert msg.content == "", "reasoning was promoted to the answer: %r" % (msg.content,)
    # Recovery ran, and it was handed the empty response, not the promoted text.
    assert seen.get("called") is True, "empty-response recovery never ran"
    assert seen.get("final_response") == ""
    assert verdict.action == "break"
    # And it is diagnosable: a WARNING naming the model and provider.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("Reasoning-only stall" in w for w in warnings)
    assert any("glm-5-3-flash-high" in w for w in warnings)
