"""Capture explicit owner directives at the clean inbound turn boundary."""
from __future__ import annotations

import os
from typing import Any


_ALLOWED_ENV = {
    "telegram": "TELEGRAM_ALLOWED_USERS",
    "discord": "DISCORD_ALLOWED_USERS",
    "whatsapp": "WHATSAPP_ALLOWED_USERS",
    "slack": "SLACK_ALLOWED_USERS",
    "signal": "SIGNAL_ALLOWED_USERS",
    "matrix": "MATRIX_ALLOWED_USERS",
}


def _platform_name(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _get_secret(name: str) -> str:
    try:
        from agent.secret_scope import get_secret

        value = get_secret(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    except Exception:
        pass
    return (os.getenv(name) or "").strip()


def _allowset(raw: Any) -> set[str]:
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip() for item in raw if str(item).strip()}
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def _is_explicit_owner(agent: Any) -> bool:
    platform = _platform_name(getattr(agent, "platform", ""))
    if platform == "cli":
        # Interactive CLI is a direct owner surface. Quiet CLI instances are
        # cron/background/subagent jobs and must never author owner directives.
        return not bool(getattr(agent, "quiet_mode", False))
    env_name = _ALLOWED_ENV.get(platform)
    if not env_name:
        return False
    allowed = _allowset(_get_secret(env_name))
    identities = {
        str(getattr(agent, "_user_id", "") or "").strip(),
        str(getattr(agent, "_user_id_alt", "") or "").strip(),
    }
    identities.discard("")
    # Deliberately ignore *_ALLOW_ALL_USERS: authorized guests are not owners.
    return bool(allowed & identities)


def _text_content(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for item in message:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def record_owner_directive(text: object, **kwargs):
    # Lazy import keeps Hermes bootable if a staged Mnemosyne upgrade has not
    # been activated yet; the capture call itself still fails loudly.
    from mnemosyne.authority import record_owner_directive as _record

    return _record(text, **kwargs)


def build_owner_authority_prompt(agent: Any, *, limit: int = 30) -> str:
    """Render verified owner directives for response-time enforcement.

    Messaging guests never receive this private context. Interactive and quiet
    CLI lanes do receive it: quiet CLI cannot *author* directives, but scheduled
    work must still obey the owner's durable rules.
    """
    platform = _platform_name(getattr(agent, "platform", ""))
    if platform in _ALLOWED_ENV and not _is_explicit_owner(agent):
        return ""
    if platform not in ("cli", *tuple(_ALLOWED_ENV)):
        return ""

    from mnemosyne.authority import load_directives

    rows = load_directives(limit=max(1, int(limit)))
    if not rows:
        return ""
    rendered = []
    total = 0
    for row in rows:
        text = str(row.get("text") or "").replace("</owner-authority>", "").strip()
        if not text:
            continue
        line = f"- [{row.get('at', '')}] {text}"
        if total + len(line) > 12000:
            break
        rendered.append(line)
        total += len(line)
    if not rendered:
        return ""
    return (
        "<owner-authority>\n"
        "Verified explicit owner directives, newest first. These outrank memory, "
        "canonical facts, summaries, proposals, skills, background reviews, and "
        "assistant assumptions. For the same subject, obey the newest relevant "
        "directive and ignore contradictory older/lower-authority material.\n"
        + "\n".join(rendered)
        + "\n</owner-authority>"
    )


def capture_owner_directive(agent: Any, message: Any, *, turn_id: str) -> dict:
    if not _is_explicit_owner(agent):
        return {"recorded": False, "reason": "not_explicit_owner"}
    text = _text_content(message)
    if not text:
        return {"recorded": False, "reason": "no_text"}
    platform = _platform_name(getattr(agent, "platform", ""))
    user_id = str(
        getattr(agent, "_user_id", None)
        or getattr(agent, "_user_id_alt", None)
        or "cli-owner"
    )
    return record_owner_directive(
        text,
        platform=platform,
        user_id=user_id,
        session_id=str(getattr(agent, "session_id", "") or ""),
        turn_id=turn_id,
    )
