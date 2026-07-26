"""Capture explicit owner directives at the clean inbound turn boundary."""
from __future__ import annotations

from typing import Any


_OWNER_PLATFORMS = frozenset({
    "telegram", "discord", "whatsapp", "slack", "signal", "matrix",
})


def _platform_name(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _owner_ids_for_platform(platform: str) -> set[str]:
    """Load the same resolved config-backed owner set used by the gateway."""
    try:
        from gateway.config import load_gateway_config
        from gateway.platforms.base import Platform

        resolved = load_gateway_config()
        platform_config = resolved.platforms.get(Platform(platform))
    except (ImportError, KeyError, TypeError, ValueError):
        return set()
    if platform_config is None:
        return set()
    extra = getattr(platform_config, "extra", None)
    raw_owner_ids = extra.get("owner_user_ids") if isinstance(extra, dict) else None
    return _allowset(raw_owner_ids) - {"*"}


def _allowset(raw: Any) -> set[str]:
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip() for item in raw if str(item).strip()}
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def is_explicit_owner_source(source: Any) -> bool:
    """Return whether a gateway source is an exact config-declared owner.

    Authorization and ownership are deliberately different: allowlists,
    allow-all guests, and paired users may chat, but only identities in
    ``gateway.platforms.<platform>.extra.owner_user_ids`` are owners.
    """
    platform = _platform_name(getattr(source, "platform", ""))
    if platform not in _OWNER_PLATFORMS:
        return False
    allowed = _owner_ids_for_platform(platform)
    identities = {
        str(getattr(source, "user_id", "") or "").strip(),
        str(getattr(source, "user_id_alt", "") or "").strip(),
    }
    identities.discard("")
    return bool(allowed & identities)


def _is_explicit_owner(agent: Any) -> bool:
    platform = _platform_name(getattr(agent, "platform", ""))
    if platform == "cli":
        # Interactive CLI is a direct owner surface. Quiet CLI instances are
        # cron/background/subagent jobs and must never author owner directives.
        return not bool(getattr(agent, "quiet_mode", False))
    source = type("OwnerSource", (), {
        "platform": platform,
        "user_id": getattr(agent, "_user_id", None),
        "user_id_alt": getattr(agent, "_user_id_alt", None),
    })()
    # Deliberately ignore *_ALLOW_ALL_USERS: authorized guests are not owners.
    return is_explicit_owner_source(source)


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
    if platform in _OWNER_PLATFORMS and not _is_explicit_owner(agent):
        return ""
    if platform not in ("cli", *tuple(_OWNER_PLATFORMS)):
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
