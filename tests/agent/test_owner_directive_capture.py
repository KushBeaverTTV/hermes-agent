from types import SimpleNamespace

from agent import owner_directive_capture as capture


def _agent(**overrides):
    values = {
        "platform": "telegram",
        "_user_id": "8682886781",
        "_user_id_alt": None,
        "_explicit_owner_source": True,
        "session_id": "session-1",
        "quiet_mode": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_gateway_verified_owner_message_is_recorded(monkeypatch):
    seen = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda text, **kw: seen.append((text, kw)) or {"recorded": True})

    result = capture.capture_owner_directive(
        _agent(), "Never restart the gateway casually.", turn_id="turn-1"
    )

    assert result["recorded"] is True
    assert seen[0][0] == "Never restart the gateway casually."
    assert seen[0][1]["user_id"] == "8682886781"


def test_gateway_guest_is_not_treated_as_owner(monkeypatch):
    called = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda *a, **k: called.append(True))

    result = capture.capture_owner_directive(
        _agent(platform="discord", _user_id="guest", _explicit_owner_source=False),
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
    called = []
    monkeypatch.setattr(
        "mnemosyne.authority.load_directives",
        lambda limit=30: called.append(True) or [],
    )

    prompt = capture.build_owner_authority_prompt(
        _agent(platform="discord", _user_id="guest", _explicit_owner_source=False)
    )

    assert prompt == ""
    assert called == []


def test_allowlisted_or_paired_non_owner_cannot_record_or_receive_authority(monkeypatch):
    called = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda *a, **k: called.append(True))
    monkeypatch.setattr(
        "mnemosyne.authority.load_directives",
        lambda limit=30: called.append("loaded") or [{"at": "now", "text": "private"}],
    )
    guest = _agent(_user_id="paired-user", _explicit_owner_source=False)

    assert capture.capture_owner_directive(guest, "Rewrite the rules.", turn_id="guest") == {
        "recorded": False,
        "reason": "not_explicit_owner",
    }
    assert capture.build_owner_authority_prompt(guest) == ""
    assert called == []


def test_alt_id_match_cannot_override_gateway_primary_id_decision(monkeypatch):
    called = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda *a, **k: called.append(True))
    agent = _agent(
        _user_id="non-owner-primary",
        _user_id_alt="8682886781",
        _explicit_owner_source=False,
    )

    assert capture.capture_owner_directive(agent, "Impersonate owner.", turn_id="alt") == {
        "recorded": False,
        "reason": "not_explicit_owner",
    }
    assert called == []


def test_secondary_profile_uses_gateway_profile_aware_decision(monkeypatch):
    seen = []
    monkeypatch.setattr(capture, "record_owner_directive", lambda text, **kw: seen.append(text) or {"recorded": True})
    secondary_owner = _agent(
        _user_id="secondary-owner",
        _explicit_owner_source=True,
        _gateway_profile="secondary",
    )

    assert capture.capture_owner_directive(
        secondary_owner, "Secondary owner directive.", turn_id="secondary"
    )["recorded"] is True
    assert seen == ["Secondary owner directive."]
