"""Regression: ``hermes update`` must resume paused Windows gateways from a ``finally``.

The pause token's ``atexit`` backstop does not cover the Windows shim hand-off
child, whose ``cmd_update`` finally exits via ``os._exit`` (no atexit handlers
run). A mid-update ``sys.exit`` there — fetch failure, shim-quarantine refusal,
install error — would strand every paused gateway until an external watchdog or
a manual restart. ``_cmd_update_impl`` therefore resumes the pause token from an
inner ``finally`` that runs before ``os._exit`` can fire.

See the Windows fleet-restart arm spec (2026-09-14 gateway-stuck incident).
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        yes=True, force=False, force_venv=False, gateway=False,
        keep_stash=False, switch_branch=False, branch=None,
    )


@pytest.fixture
def update_harness(tmp_path, monkeypatch):
    """Drive ``_cmd_update_impl`` to the git-fetch step with a paused gateway token."""
    from hermes_cli import gitlock, update_cmd, update_receipt
    from hermes_cli import main as hm

    project_root = tmp_path / "hermes-agent"
    (project_root / ".git").mkdir(parents=True)

    token = {"resume_needed": True, "profiles": {"work": 4321}, "unmapped_pids": [], "unmapped": []}
    resumes: list[dict] = []

    monkeypatch.setattr(hm, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(hm, "_is_windows", lambda: True)
    monkeypatch.setattr(hm, "_capture_active_lazy_features", lambda: None)
    monkeypatch.setattr(hm, "_capture_active_tool_dependencies", lambda: None)
    monkeypatch.setattr(hm, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(hm, "_pause_windows_gateways_for_update", lambda: dict(token))
    monkeypatch.setattr(
        hm, "_resume_windows_gateways_after_update",
        lambda tok: resumes.append(tok))
    monkeypatch.setattr(hm, "_venv_scripts_dir", lambda: project_root / "venv" / "Scripts")
    monkeypatch.setattr(hm, "_get_origin_url", lambda *_a: "")
    monkeypatch.setattr(hm, "_resolve_update_branch", lambda _args: "main")
    monkeypatch.setattr(hm, "_warn_orphaned_update_autostashes", lambda *_a: None)

    monkeypatch.setattr(update_cmd, "_read_project_version", lambda: "0.0.0")
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {})
    monkeypatch.setattr(update_cmd, "_clear_windows_venv_holders_or_exit", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_desktop_app_present", lambda *_a: False)
    monkeypatch.setattr(update_cmd, "_ensure_non_trampoline_git", lambda cmd: cmd)
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *_a: None)
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *_a: None)
    monkeypatch.setattr(update_cmd, "_is_fork", lambda *_a: False)
    monkeypatch.setattr(update_receipt, "begin_update_receipt", lambda *a, **k: None)

    # The impl registers the resume as an atexit backstop; swallow the registration
    # here so the patched resume doesn't leak into pytest's own interpreter exit.
    import atexit as _atexit
    monkeypatch.setattr(_atexit, "register", lambda *a, **k: None)

    monkeypatch.setattr(gitlock, "clear_stale_git_locks", lambda *_a: [])
    monkeypatch.setattr(gitlock, "clear_stale_tmp_packs", lambda *_a: [])
    monkeypatch.setattr(gitlock, "repair_broken_shallow_boundaries", lambda *_a: 0)
    monkeypatch.setattr(gitlock, "prune_stale_shallow_grafts", lambda *_a: 0)

    return SimpleNamespace(
        hm=hm, update_cmd=update_cmd, token=token, resumes=resumes,
        project_root=project_root, monkeypatch=monkeypatch,
    )


def _fail_fetch(h, *, raise_exc=None):
    """Patch ``_git_run`` so ``fetch`` fails; every other git call succeeds."""
    def fake_git_run(git_cmd, args, cwd=None, *, check=False, network=False):
        if args and args[0] == "fetch":
            if raise_exc is not None:
                raise raise_exc
            return subprocess.CompletedProcess(git_cmd + args, 128, stdout="", stderr="fetch: boom")
        return subprocess.CompletedProcess(git_cmd + args, 0, stdout="", stderr="")

    h.monkeypatch.setattr(h.update_cmd, "_git_run", fake_git_run)


def test_fetch_failure_still_resumes_paused_gateways(update_harness):
    """``sys.exit(1)`` on fetch failure must not leave the paused fleet down."""
    h = update_harness
    _fail_fetch(h)

    with pytest.raises(SystemExit) as excinfo:
        h.update_cmd._cmd_update_impl(_args(), gateway_mode=False)

    assert excinfo.value.code == 1
    assert h.resumes and h.resumes[0]["resume_needed"], (
        "paused gateways were not resumed before the update exited")


def test_crash_mid_update_still_resumes_paused_gateways(update_harness):
    """A non-SystemExit crash mid-update also triggers the finally resume."""
    h = update_harness
    _fail_fetch(h, raise_exc=RuntimeError("simulated mid-update crash"))

    with pytest.raises(RuntimeError, match="simulated mid-update crash"):
        h.update_cmd._cmd_update_impl(_args(), gateway_mode=False)

    assert h.resumes and h.resumes[0]["resume_needed"]


def test_failed_install_still_resumes_paused_gateways(update_harness):
    """An install failure (ZIP fallback raising) restores pre-update gateway state."""
    h = update_harness
    install_error = subprocess.CalledProcessError(1, ["git", "fetch"], stderr="boom")
    _fail_fetch(h, raise_exc=install_error)

    def failing_zip(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["uv", "pip", "install"], stderr="zip failed")

    h.monkeypatch.setattr(h.update_cmd, "_update_via_zip", failing_zip)

    with pytest.raises(subprocess.CalledProcessError):
        h.update_cmd._cmd_update_impl(_args(), gateway_mode=False)

    assert h.resumes and h.resumes[0]["resume_needed"]


def test_resume_failure_does_not_mask_update_exit(update_harness):
    """A failing resume warns but never replaces the update's own exit code."""
    h = update_harness
    _fail_fetch(h)
    h.monkeypatch.setattr(
        h.hm, "_resume_windows_gateways_after_update",
        lambda tok: (_ for _ in ()).throw(RuntimeError("resume exploded")))

    with pytest.raises(SystemExit) as excinfo:
        h.update_cmd._cmd_update_impl(_args(), gateway_mode=False)

    assert excinfo.value.code == 1


class TestResumeInFinallyHelper:
    """Unit contract for ``_resume_windows_gateways_in_finally``."""

    def test_none_token_is_noop(self, monkeypatch):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        called = []
        monkeypatch.setattr(hm, "_resume_windows_gateways_after_update", lambda tok: called.append(tok))
        update_cmd._resume_windows_gateways_in_finally(None)
        assert called == []

    def test_token_is_resumed(self, monkeypatch):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        called = []
        monkeypatch.setattr(hm, "_resume_windows_gateways_after_update", lambda tok: called.append(tok))
        token = {"resume_needed": True, "profiles": {"work": 1}}
        update_cmd._resume_windows_gateways_in_finally(token)
        assert called == [token]

    def test_resume_error_is_swallowed_with_warning(self, monkeypatch, capsys):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        def boom(_tok):
            raise RuntimeError("resume exploded")

        monkeypatch.setattr(hm, "_resume_windows_gateways_after_update", boom)
        update_cmd._resume_windows_gateways_in_finally({"resume_needed": True})
        assert "Recover with: hermes gateway restart" in capsys.readouterr().out
