"""Regression coverage for per-call and per-task delegation routing.

These tests protect the owner-required ability to mix model/provider routes in one
``delegate_task`` call and to attribute the resulting work to the routes that
actually ran.  They must not contact external providers.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import tools.delegate_tool as dt
from tools.process_registry import _format_async_delegation


def _parent() -> MagicMock:
    parent = MagicMock()
    parent.base_url = "https://chatgpt.com/backend-api/codex"
    parent.api_key = "test-parent-key"
    parent.provider = "openai-codex"
    parent.api_mode = "codex_responses"
    parent.model = "gpt-5.6-sol"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._interrupt_requested = False
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._memory_manager = None
    parent.session_id = "parent-multimodel-test"
    parent.session_estimated_cost_usd = 0.0
    return parent


def _creds(model: str | None, provider: str | None) -> dict:
    return {
        "model": model,
        "provider": provider,
        "base_url": f"https://{provider}.invalid/v1" if provider else None,
        "api_key": f"test-{provider}-key" if provider else None,
        "api_mode": "chat_completions",
        "request_overrides": None,
        "max_output_tokens": None,
        "command": None,
        "args": None,
    }


def test_model_facing_schema_exposes_top_level_and_per_task_routes():
    props = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    task_props = props["tasks"]["items"]["properties"]

    assert "model" in props
    assert "provider" in props
    assert "model" in task_props
    assert "provider" in task_props

    dynamic = dt._build_dynamic_schema_overrides()
    assert "PER-TASK MODELS ARE SUPPORTED" in dynamic["description"]
    assert "different route" in dynamic["parameters"]["properties"]["tasks"]["description"]


def test_sol_pro_alias_never_downgrades_to_plain_sol():
    model, provider = dt._expand_model_provider_alias("sol-pro", None)

    assert model == "gpt-5.6-sol-pro"
    assert provider == "openai-codex"


def test_task_provider_override_is_resolved_independently(monkeypatch):
    assert hasattr(dt, "_resolve_task_credentials")
    seen = []

    def fake_resolve(cfg, parent_agent):
        seen.append(dict(cfg))
        return _creds(cfg.get("model"), cfg.get("provider"))

    monkeypatch.setattr(dt, "_resolve_delegation_credentials", fake_resolve)
    default = _creds("gpt-5.6-sol", "openai-codex")
    base_cfg = {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_key": "must-not-leak-across-providers",
        "api_mode": "codex_responses",
    }

    resolved = dt._resolve_task_credentials(
        {"model": "grok-4.5", "provider": "xai-oauth"},
        base_cfg,
        _parent(),
        default,
    )

    assert resolved["model"] == "grok-4.5"
    assert resolved["provider"] == "xai-oauth"
    assert seen == [
        {
            "model": "grok-4.5",
            "provider": "xai-oauth",
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }
    ]


def test_delegate_batch_builds_each_child_with_its_own_route(monkeypatch):
    builds = []

    monkeypatch.setattr(
        dt,
        "_load_config",
        lambda: {
            "model": "gpt-5.6-sol",
            "provider": "openai-codex",
            "max_iterations": 4,
            "max_concurrent_children": 8,
        },
    )
    monkeypatch.setattr(
        dt,
        "_resolve_delegation_credentials",
        lambda cfg, parent_agent: _creds(cfg.get("model"), cfg.get("provider")),
    )

    def fake_build(**kwargs):
        builds.append(dict(kwargs))
        return SimpleNamespace(
            model=kwargs["model"],
            provider=kwargs["override_provider"],
            _delegate_role=kwargs["role"],
            _delegate_saved_tool_names=[],
            tool_progress_callback=None,
            session_estimated_cost_usd=0.0,
        )

    monkeypatch.setattr(dt, "_build_child_agent", fake_build)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda task_index, goal, child, parent_agent: {
            "task_index": task_index,
            "status": "completed",
            "summary": goal,
            "api_calls": 1,
            "duration_seconds": 0.01,
            "model": child.model,
            "provider": child.provider,
            "exit_reason": "completed",
            "_child_role": child._delegate_role,
            "_child_cost_usd": 0.0,
        },
    )

    out = json.loads(
        dt.delegate_task(
            tasks=[
                {"goal": "research", "model": "grok-4.5", "provider": "xai-oauth"},
                {"goal": "design", "model": "k3", "provider": "kimi-coding"},
            ],
            parent_agent=_parent(),
        )
    )

    assert [(b["model"], b["override_provider"]) for b in builds] == [
        ("grok-4.5", "xai-oauth"),
        ("k3", "kimi-coding"),
    ]
    assert [(r["model"], r["provider"]) for r in out["results"]] == [
        ("grok-4.5", "xai-oauth"),
        ("k3", "kimi-coding"),
    ]


def test_run_single_child_reports_actual_provider():
    child = MagicMock()
    child.model = "grok-4.5"
    child.provider = "xai-oauth"
    child._credential_pool = None
    child.session_prompt_tokens = 10
    child.session_completion_tokens = 5
    child.session_estimated_cost_usd = 0.0
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [],
    }

    result = dt._run_single_child(0, "research", child, _parent())

    assert result["model"] == "grok-4.5"
    assert result["provider"] == "xai-oauth"


def test_async_batch_attribution_uses_actual_child_routes():
    text = _format_async_delegation(
        {
            "delegation_id": "deleg_multimodel",
            "is_batch": True,
            "role": "leaf",
            "model": "gpt-5.6-sol",  # deliberately stale global pin
            "status": "completed",
            "goals": ["research", "design"],
            "results": [
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "research complete",
                    "model": "grok-4.5",
                    "provider": "xai-oauth",
                },
                {
                    "task_index": 1,
                    "status": "completed",
                    "summary": "design complete",
                    "model": "k3",
                    "provider": "kimi-coding",
                },
            ],
            "total_duration_seconds": 1.0,
        }
    )

    assert "Role: leaf   Model: grok-4.5 + k3" in text
    assert "route=xai-oauth/grok-4.5" in text
    assert "route=kimi-coding/k3" in text
    assert "Role: leaf   Model: gpt-5.6-sol" not in text
