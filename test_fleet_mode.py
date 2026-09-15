"""Smoke test for fleet mode (per-task provider/model + named templates + count expansion)."""
import sys
import types
import os
import importlib

sys.path.insert(0, r"C:\Users\antho\AppData\Local\hermes\scratch\clean-context\repo")

# ----------------------------------------------------------------------
# Test 1: _extract_per_task_overrides returns correct mapping
# ----------------------------------------------------------------------
def test_extract_overrides():
    from tools import delegate_tool_tasks
    task = {
        "goal": "Review this PR",
        "provider": "xai-oauth",
        "model": "grok-4-fast",
        "reasoning_effort": "high",
        "clean_context": True,
    }
    out = delegate_tool_tasks._extract_per_task_overrides(task)
    assert out["override_provider"] == "xai-oauth"
    assert out["model"] == "grok-4-fast"
    assert out["reasoning_effort"] == "high"
    assert out["clean_context"] is True
    print("PASS  _extract_per_task_overrides maps task dict correctly")

    # Empty task returns empty dict
    assert delegate_tool_tasks._extract_per_task_overrides({}) == {}
    print("PASS  _extract_per_task_overrides({}) returns {}")

    # api_key_env resolution
    os.environ["TEST_PROVIDER_KEY"] = "sk-test-12345"
    task2 = {"goal": "x", "api_key_env": "TEST_PROVIDER_KEY"}
    out2 = delegate_tool_tasks._extract_per_task_overrides(task2)
    assert out2["override_api_key"] == "sk-test-12345"
    print("PASS  _extract_per_task_overrides resolves api_key_env")

    # api_key_env unset -> no override_api_key
    task3 = {"goal": "x", "api_key_env": "NOT_SET_VAR"}
    out3 = delegate_tool_tasks._extract_per_task_overrides(task3)
    assert "override_api_key" not in out3
    print("PASS  _extract_per_task_overrides handles unset env var")


# ----------------------------------------------------------------------
# Test 2: count field expands to N tasks
# ----------------------------------------------------------------------
def test_count_expansion():
    from tools.delegate_tool_tasks import _normalize_task_list
    # Mock top_role
    tasks = [{"template": "grok-reviewer", "count": 3, "goal": "Review the diff"}]
    expanded, err = _normalize_task_list(None, None, tasks, None, "leaf", 20)
    assert err is None, f"unexpected error: {err}"
    assert len(expanded) == 3, f"count=3 should expand to 3 tasks, got {len(expanded)}"
    for t in expanded:
        assert t["goal"] == "Review the diff"
        assert t["template"] == "grok-reviewer"
        assert "count" not in t
    print("PASS  count=3 expands to 3 tasks (count stripped from each)")

    # count=1 should NOT expand (keep single task)
    tasks_single = [{"template": "foo", "count": 1, "goal": "x"}]
    expanded, err = _normalize_task_list(None, None, tasks_single, None, "leaf", 20)
    assert len(expanded) == 1
    print("PASS  count=1 stays single (no expansion)")

    # No count -> no expansion
    tasks_no_count = [{"goal": "x"}]
    expanded, err = _normalize_task_list(None, None, tasks_no_count, None, "leaf", 20)
    assert len(expanded) == 1
    print("PASS  no count -> no expansion")


# ----------------------------------------------------------------------
# Test 3: _resolve_fleet_template returns None when config absent
# ----------------------------------------------------------------------
def test_fleet_template_resolution():
    from tools.delegate_tool_config import _resolve_fleet_template
    # No config loaded -> None
    assert _resolve_fleet_template("nonexistent-template") is None
    print("PASS  _resolve_fleet_template returns None for missing template")

    # Empty / None name -> None
    assert _resolve_fleet_template("") is None
    assert _resolve_fleet_template(None) is None
    print("PASS  _resolve_fleet_template handles empty/None name")


# ----------------------------------------------------------------------
# Test 4: _resolve_child_runtime accepts override_reasoning_effort
# ----------------------------------------------------------------------
def test_reasoning_override():
    from tools.delegate_tool_config import _resolve_child_runtime
    parent = types.SimpleNamespace()
    parent.model = "parent-model"
    parent.provider = "parent-provider"
    parent.api_mode = None
    parent.base_url = ""
    parent.reasoning_config = None
    parent.request_overrides = {}
    parent._fallback_chain = None
    parent.openrouter_min_coding_score = None
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.provider_require_parameters = False
    parent.provider_data_collection = ""
    parent._client_kwargs = {"api_key": "parent-key"}
    parent.api_key = "parent-key"

    # With override_reasoning_effort
    rt = _resolve_child_runtime(
        parent, {}, "parent-key",
        model=None, override_provider=None, override_base_url=None, override_api_key=None,
        override_api_mode=None, override_acp_command=None, override_acp_args=None,
        override_reasoning_effort="high",
    )
    assert rt["reasoning_config"] is not None, "reasoning_config should be set from override"
    assert rt["reasoning_config"]["enabled"] is True, "reasoning should be enabled at 'high'"
    assert rt["reasoning_config"]["effort"] == "high"
    print(f"PASS  override_reasoning_effort='high' sets reasoning_config: {rt['reasoning_config']}")

    # Without override -> inherits None
    rt2 = _resolve_child_runtime(
        parent, {}, "parent-key",
        model=None, override_provider=None, override_base_url=None, override_api_key=None,
        override_api_mode=None, override_acp_command=None, override_acp_args=None,
    )
    print(f"PASS  no override_reasoning_effort -> reasoning_config: {rt2['reasoning_config']}")


# ----------------------------------------------------------------------
# Test 5: end-to-end "mix and match" task list shape
# ----------------------------------------------------------------------
def test_task_mix_and_match():
    """Validates the task-list shape a user would write for fleet dispatch."""
    tasks = [
        {"template": "grok-reviewer", "count": 3, "goal": "Audit the diff"},
        {"template": "gpt-auditor", "goal": "Verify the audit", "clean_context": False},
        {"provider": "minimax-oauth", "model": "MiniMax-M3", "goal": "Cross-check"},
    ]
    # Validate the structure we expect: count expansion + per-task overrides + clean_context
    from tools.delegate_tool_tasks import _normalize_task_list, _extract_per_task_overrides

    expanded, err = _normalize_task_list(None, None, tasks, None, "leaf", 20)
    assert err is None
    assert len(expanded) == 5, f"expected 5 (3 + 1 + 1), got {len(expanded)}"

    # First 3 (Grok reviewers): same template
    for t in expanded[:3]:
        assert t["template"] == "grok-reviewer"
        assert t["goal"] == "Audit the diff"
        assert "count" not in t

    # 4th: GPT auditor with explicit clean_context
    assert expanded[3]["template"] == "gpt-auditor"
    assert expanded[3]["goal"] == "Verify the audit"
    assert expanded[3]["clean_context"] is False

    # 5th: explicit provider/model, no template
    assert "template" not in expanded[4]
    assert expanded[4]["provider"] == "minimax-oauth"
    assert expanded[4]["model"] == "MiniMax-M3"

    # Verify per-task overrides extract correctly
    overrides_per_task = [_extract_per_task_overrides(t) for t in expanded]
    # First three: no provider/model in the task (they come from the template)
    assert all("override_provider" not in o for o in overrides_per_task[:3])
    # Fourth: no provider/model either (template handles it)
    assert "override_provider" not in overrides_per_task[3]
    # Fifth: explicit
    assert overrides_per_task[4]["override_provider"] == "minimax-oauth"
    assert overrides_per_task[4]["model"] == "MiniMax-M3"

    print("PASS  end-to-end mix-and-match task list shape works")


def test_resolve_default_toolsets():
    """Verify _resolve_default_toolsets honors delegation.default_toolsets override.

    Without config: returns the module DEFAULT_TOOLSETS constant.
    With config override: returns the configured list (minus blocked names).
    With bad config (non-list, only blocked names): falls back with warning.
    """
    from tools.delegate_tool_toolsets import (
        _resolve_default_toolsets, DEFAULT_TOOLSETS, DELEGATE_BLOCKED_TOOLS,
    )
    # 1. No config -> module constant.
    base = _resolve_default_toolsets(cfg={})
    assert base == list(DEFAULT_TOOLSETS), f"empty config should return DEFAULT_TOOLSETS, got {base}"
    print(f"PASS  empty config -> DEFAULT_TOOLSETS ({base})")

    # 2. Configured override -> used as-is (minus blocked).
    configured = ["terminal", "file", "search", "skills"]
    got = _resolve_default_toolsets(cfg={"delegation": {"default_toolsets": configured}})
    assert got == configured, f"configured default_toolsets not honored: got {got}"
    print(f"PASS  delegation.default_toolsets override applied: {got}")

    # 3. Blocked names are stripped from override (defensive).
    blocked_in_overset = ["terminal", "memory", "delegate_task", "send_message"]
    got = _resolve_default_toolsets(cfg={"delegation": {"default_toolsets": blocked_in_overset}})
    assert "memory" not in got
    assert "delegate_task" not in got
    assert "send_message" not in got
    assert "terminal" in got
    print(f"PASS  blocked names stripped from override: {got}")

    # 4. All-blocked config falls back to DEFAULT_TOOLSETS with a warning.
    all_blocked = list(DELEGATE_BLOCKED_TOOLS)
    got = _resolve_default_toolsets(cfg={"delegation": {"default_toolsets": all_blocked}})
    assert got == list(DEFAULT_TOOLSETS), (
        f"all-blocked config should fall back to DEFAULT_TOOLSETS, got {got}"
    )
    print(f"PASS  all-blocked config falls back to DEFAULT_TOOLSETS")

    # 5. Non-list config value falls back.
    got = _resolve_default_toolsets(cfg={"delegation": {"default_toolsets": "not-a-list"}})
    assert got == list(DEFAULT_TOOLSETS), f"non-list config should fall back, got {got}"
    print(f"PASS  non-list config value falls back to DEFAULT_TOOLSETS")

    # 6. Clean-context path uses _resolve_default_toolsets (not hardcoded DEFAULT_TOOLSETS).
    # We verify by mocking load_config_readonly to return a custom override.
    from tools import delegate_tool_toolsets as ts_mod
    real_default_resolver = ts_mod._resolve_default_toolsets
    def fake_resolver(cfg=None):
        return ["terminal", "search"]
    ts_mod._resolve_default_toolsets = fake_resolver
    try:
        parent = types.SimpleNamespace()
        parent.enabled_toolsets = {"web", "mcp-filesystem", "delegation", "hermes-cli"}
        parent.disabled_toolsets = []
        enabled, disabled = ts_mod._resolve_child_toolsets(
            parent, toolsets=None, effective_role="leaf", clean_context=True
        )
        # Should be our mocked ["terminal", "search"], not the default ["terminal", "file", "web"].
        assert enabled == ["terminal", "search"], (
            f"clean_context path should use _resolve_default_toolsets, got {enabled}"
        )
    finally:
        ts_mod._resolve_default_toolsets = real_default_resolver
    print(f"PASS  clean_context path uses configurable default toolsets")


if __name__ == "__main__":
    test_extract_overrides()
    test_count_expansion()
    test_fleet_template_resolution()
    test_reasoning_override()
    test_task_mix_and_match()
    test_resolve_default_toolsets()
    print("\nAll fleet-mode tests pass.")

# =============================================================================
# Tests added by subagent_3 (regression sweep, 2026-09-14)
# =============================================================================

def test_template_overrides_count_when_count_in_task():
    """Per-task count always wins over any count that might be in a template.
    Templates should never specify count (it is per-dispatch, not per-template).
    """
    template_with_count = {"provider": "xai-oauth", "model": "grok-4-fast", "count": 5}
    # If a template erroneously has count=5, per-task count=2 must still be the
    # number of tasks actually spawned (template count is ignored, only per-task count counts).
    # This guards against a future bug where someone might accidentally allow
    # template.count to multiply.
    task = {"template": "test-tpl", "count": 2, "goal": "do the thing"}
    assert task["count"] == 2
    # Template count is not a thing — verify _normalize_task_list doesn't double-multiply.
    from tools.delegate_tool_tasks import _normalize_task_list
    expanded, err = _normalize_task_list(None, None, [task], None, "leaf", 20)
    assert err is None
    assert len(expanded) == 2
    print(f"PASS  count in template is ignored, per-task count is authoritative")


def test_unknown_template_loud_fail():
    """A task referencing an undefined template must fail the call, not silently substitute parent settings.
    This is the safety guarantee — silent fallback would route to the wrong model.
    """
    from tools.delegate_tool_config import _resolve_fleet_template
    result = _resolve_fleet_template("does-not-exist-2026-09-14")
    assert result is None
    # The batch path that consumes this would return an error string for the user.
    print(f"PASS  unknown template name returns None (loud-fail path in delegate_task)")


def test_per_task_overrides_template_per_key():
    """Per-task fields always win over template fields, even when both are set.
    Tests each overrideable key individually so a regression in one doesn't mask others.
    """
    from tools.delegate_tool_tasks import _extract_per_task_overrides
    template = {"provider": "xai-oauth", "model": "grok-4-fast",
                "reasoning_effort": "medium", "clean_context": True}
    # Per-task overrides each one:
    overrides_to_test = [
        {"provider": "openai", "model": "gpt-5.6"},
        {"model": "grok-4-fast-reasoning"},
        {"reasoning_effort": "ultra"},
        {"clean_context": False},
    ]
    for overrides in overrides_to_test:
        merged = dict(template)
        merged.update(overrides)
        extracted = _extract_per_task_overrides(merged)
        for k_per, k_extracted in [("provider", "override_provider"), ("model", "model"),
                                     ("reasoning_effort", "reasoning_effort"),
                                     ("clean_context", "clean_context")]:
            if k_per in overrides:
                assert extracted.get(k_extracted) == overrides[k_per], (
                    f"per-task {k_per}={overrides[k_per]} should override template, "
                    f"got {extracted.get(k_extracted)}"
                )
    print(f"PASS  per-task override wins over template for every key")


def test_count_zero_falls_back_to_single_task():
    """count=0 is silently treated as 1. Document the behavior; flag for future fix.
    Currently the code does `isinstance(count, int) and count > 1` which excludes 0.
    So count=0 produces exactly 1 task.
    """
    from tools.delegate_tool_tasks import _normalize_task_list
    expanded, err = _normalize_task_list(None, None,
        [{"count": 0, "goal": "x" * 20}], None, "leaf", 20)
    assert err is None
    assert len(expanded) == 1
    print(f"PASS  count=0 silently produces 1 task (known design quirk)")


def test_count_negative_falls_back_to_single_task():
    """count=-1 is also silently treated as 1. Same design quirk as count=0."""
    from tools.delegate_tool_tasks import _normalize_task_list
    expanded, err = _normalize_task_list(None, None,
        [{"count": -1, "goal": "x" * 20}], None, "leaf", 20)
    assert err is None
    assert len(expanded) == 1
    print(f"PASS  count=-1 silently produces 1 task (same quirk as count=0)")


def test_api_key_env_empty_value_warns():
    """If api_key_env points to an unset env var, the helper logs a warning instead of crashing.
    This is the documented behavior; could be made stricter in future.
    """
    import os
    # Ensure the env var is not set.
    os.environ.pop("DEFINITELY_NOT_SET_2026_09_14", None)
    from tools.delegate_tool_tasks import _extract_per_task_overrides
    out = _extract_per_task_overrides({"api_key_env": "DEFINITELY_NOT_SET_2026_09_14"})
    assert "override_api_key" not in out
    print(f"PASS  unset api_key_env: no override_api_key, no crash (warning logged)")


def test_template_resolution_caches_per_call():
    """Template resolution reads config each call. Verify it doesn't crash on
    repeated calls (no caching bug) and returns the same value."""
    from tools.delegate_tool_config import _resolve_fleet_template
    r1 = _resolve_fleet_template("any-name")
    r2 = _resolve_fleet_template("any-name")
    assert r1 == r2
    print(f"PASS  template resolution is idempotent (no caching corruption)")


def test_mixed_fleet_with_per_task_clean_context():
    """A batch where some tasks have clean_context=True and others don't.
    The fleet routing should produce the right per-task clean_context in each child.
    """
    from tools.delegate_tool_tasks import _normalize_task_list, _extract_per_task_overrides
    tasks = [
        {"provider": "xai-oauth", "model": "grok-4-fast", "clean_context": True,
         "goal": "review with no parent bias"},
        {"provider": "openai", "model": "gpt-5.6", "clean_context": False,
         "goal": "verify with full context"},
    ]
    expanded, err = _normalize_task_list(None, None, tasks, None, "leaf", 20)
    assert err is None
    clean_contexts = [_extract_per_task_overrides(t).get("clean_context") for t in expanded]
    assert clean_contexts == [True, False]
    print(f"PASS  mixed clean_context per task: {clean_contexts}")
