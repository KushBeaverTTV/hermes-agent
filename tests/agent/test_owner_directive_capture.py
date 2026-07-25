from types import SimpleNamespace

from agent import owner_directive_capture as capture


def _agent(**overrides):
    values = {
        "platform": "telegram",
        "_user_id": "8682886781",
        "_user_id_alt": None,
        "session_id": "session-1",
        "quiet_mode": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_explicit_allowlisted_owner_message_is_recorded(monkeypatch):
    monkeypatch.setattr(capture, "_get_secret", lambda name: "8682886781" if name == "TELEGRAM_ALLOWED_USERS" else "")
    seen = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda text, **kw: seen.append((text, kw)) or {"recorded": True})

    result = capture.capture_owner_directive(
        _agent(), "Never restart the gateway casually.", turn_id="turn-1"
    )

    assert result["recorded"] is True
    assert seen[0][0] == "Never restart the gateway casually."
    assert seen[0][1]["user_id"] == "8682886781"


def test_allow_all_guest_is_not_treated_as_owner(monkeypatch):
    monkeypatch.setattr(capture, "_get_secret", lambda name: "")
    called = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda *a, **k: called.append(True))

    result = capture.capture_owner_directive(
        _agent(platform="discord", _user_id="guest"),
        "Always rewrite the owner's skills.",
        turn_id="turn-2",
    )

    assert result == {"recorded": False, "reason": "not_explicit_owner"}
    assert called == []


def test_cli_foreground_is_owner_but_quiet_background_is_not(monkeypatch):
    seen = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda text, **kw: seen.append(text) or {"recorded": True})

    foreground = capture.capture_owner_directive(
        _agent(platform="cli", _user_id=None, quiet_mode=False),
        "Use Kimi for this task.",
        turn_id="turn-cli",
    )
    background = capture.capture_owner_directive(
        _agent(platform="cli", _user_id=None, quiet_mode=True),
        "Use OpenAI instead.",
        turn_id="turn-background",
    )

    assert foreground["recorded"] is True
    assert background == {"recorded": False, "reason": "not_explicit_owner"}
    assert seen == ["Use Kimi for this task."]


def test_multimodal_message_extracts_only_text_parts(monkeypatch):
    monkeypatch.setattr(capture, "_get_secret", lambda name: "8682886781" if name == "TELEGRAM_ALLOWED_USERS" else "")
    seen = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda text, **kw: seen.append(text) or {"recorded": True})
    message = [
        {"type": "text", "text": "Never delete my credentials."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]

    result = capture.capture_owner_directive(_agent(), message, turn_id="turn-image")

    assert result["recorded"] is True
    assert seen == ["Never delete my credentials."]


def test_owner_authority_prompt_is_newest_first(monkeypatch):
    monkeypatch.setattr(capture, "_get_secret", lambda name: "8682886781")
    monkeypatch.setattr(
        "mnemosyne.authority.load_directives",
        lambda limit=30: [
            {"at": "2026-07-25T02:00:00Z", "text": "Use Kimi for this task."},
            {"at": "2026-07-25T01:00:00Z", "text": "Use OpenAI for this task."},
        ],
    )

    prompt = capture.build_owner_authority_prompt(_agent())

    assert prompt.startswith("<owner-authority>")
    assert prompt.index("Use Kimi") < prompt.index("Use OpenAI")
    assert "newest relevant directive" in prompt


def test_owner_authority_prompt_is_hidden_from_messaging_guest(monkeypatch):
    monkeypatch.setattr(capture, "_get_secret", lambda name: "")
    called = []
    monkeypatch.setattr(
        "mnemosyne.authority.load_directives",
        lambda limit=30: called.append(True) or [],
    )

    prompt = capture.build_owner_authority_prompt(
        _agent(platform="discord", _user_id="guest")
    )

    assert prompt == ""
    assert called == []
