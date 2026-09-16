"""OpenAI-compatible shim that forwards Hermes requests to `devin acp`.

Each request starts a short-lived ACP session, sends the formatted conversation
as one prompt, collects text chunks, and returns the minimal OpenAI-client shape.
Modeled on `copilot_acp_client.py`; differences:
  * CLI is `devin acp` (not `copilot --acp --stdio`)
  * No `--acp` capability probe — `devin` advertises `acp` as a fixed subcommand
  * NDJSON framing (one JSON-RPC per line), same as Copilot ACP
  * No MCP server plumbing in-session — Devin handles its own MCP servers

Out-of-tree in spirit: lives in `agent/` next to its sibling so the plugin's
create_client can do `from agent.devin_acp_client import DevinACPClient`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import shlex
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.acp_openai_bridge import (
    completion_to_stream_chunks as _completion_to_stream_chunks,
    extract_tool_calls_from_text as _extract_tool_calls_from_text,
    render_tool_bridge_sections as _render_tool_bridge_sections,
)
from tools.environments.local import hermes_subprocess_env

ACP_MARKER_BASE_URL = "acp://devin"
logger = logging.getLogger(__name__)
_DEFAULT_TIMEOUT_SECONDS = 900.0
_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool", "context": "Context"}
_PROMPT_PREAMBLE = (
    "You are being used as the active ACP agent backend for Hermes.",
    "Use ACP capabilities to complete tasks.",
    "IMPORTANT: If you take an action with a tool, you MUST output tool calls using <​tool_call>{...}</​tool_call> blocks with JSON exactly in OpenAI function-call shape.",
    "If no tool is needed, answer normally.",
)

# Initialize params exactly per the ACP spec example that Devin accepts:
# id=0, protocolVersion=1, terminal capability, full clientInfo shape.
_INITIALIZE_PARAMS = dict(
    protocolVersion=1,
    clientCapabilities={"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True},
    clientInfo={"name": "hermes-agent", "title": "Hermes Agent", "version": "0.0.0"},
)


def _resolve_command() -> str:
    return os.getenv("HERMES_DEVIN_ACP_COMMAND", "").strip() or "devin"


def _resolve_args() -> list[str]:
    return shlex.split(os.getenv("HERMES_DEVIN_ACP_ARGS", "").strip()) or ["acp"]


def _build_subprocess_env() -> dict[str, str]:
    """Devin subprocess env — inherits nothing user-specific. Devin's CLI owns
    its own auth via the credentials.toml it already wrote."""
    env = hermes_subprocess_env(inherit_credentials=False)
    # The Devin CLI logs in via the existing user-level credentials file at
    # $APPDATA/devin/credentials.toml. No env var needed.
    return env


def _jsonrpc_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _enabled_id_list(entries: Any, key: str) -> list[str]:
    """Ordered ids whose enablement flag is not 'disabled'."""
    seen: set[str] = set()
    result: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get(key) or "").strip()
        if not value or value in seen:
            continue
        meta = entry.get("_meta") or {}
        enabled = str(meta.get("cognition.ai/enablement") or meta.get("enablement") or "").strip().lower()
        if enabled == "disabled":
            continue
        seen.add(value)
        result.append(value)
    return result


def _model_config_option(session: dict[str, Any]) -> dict[str, Any] | None:
    """Find the model config option in session/new response.

    Devin (like Copilot) advertises a configOption with category/id='model'.
    """
    for option in session.get("configOptions") or []:
        if not isinstance(option, dict):
            continue
        cat = option.get("category") or option.get("id") or ""
        if "model" in str(cat).lower():
            return option
    return None


def _session_model_ids(session: dict[str, Any]) -> list[str]:
    if option := _model_config_option(session):
        return _enabled_id_list(option.get("options") or option.get("values") or [], "value")
    return _enabled_id_list((session.get("models") or {}).get("availableModels"), "modelId")


def _model_selection_request(session: dict[str, Any], requested_model: str) -> tuple[str, dict[str, str]] | None:
    """Pick the right ACP method to set the model for this session."""
    session_id = str(session.get("sessionId") or "").strip()
    requested_model = str(requested_model or "").strip()
    if not session_id or not requested_model or requested_model == "devin":
        return None
    if option := _model_config_option(session):
        available = _enabled_id_list(option.get("options") or option.get("values") or [], "value")
        if available and requested_model not in available:
            return None
        return ("session/set_config_option", {
            "sessionId": session_id,
            "configId": str(option.get("id") or "model"),
            "value": requested_model,
        })
    available = _enabled_id_list((session.get("models") or {}).get("availableModels"), "modelId")
    if available and requested_model not in available:
        return None
    return ("session/set_model", {"sessionId": session_id, "modelId": requested_model})


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        return content["content"].strip() if isinstance(content.get("content"), str) else json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                parts.append(item["text"].strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _format_messages_as_prompt(
    messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, tool_choice: Any = None,
) -> str:
    """Render messages as one big prompt string Devin can answer."""
    # Deliberately omit a "requested model" line — Devin picks it via ACP set_model.
    sections: list[str] = [*_PROMPT_PREAMBLE, *_render_tool_bridge_sections(tools, tool_choice)]
    transcript: list[str] = []
    for message in (m for m in messages if isinstance(m, dict)):
        role = str(message.get("role") or "unknown").strip().lower()
        if rendered := _render_message_content(message.get("content")):
            transcript.append(f"{_ROLE_LABELS.get(role, 'Context')}:\n{rendered}")
    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))
    sections.append("Continue the conversation from the latest user request.")
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _effective_timeout(timeout: Any) -> float:
    if isinstance(timeout, (int, float)):
        return float(timeout)
    candidates = [getattr(timeout, attr, None) for attr in ("read", "write", "connect", "pool", "timeout")]
    return max((float(v) for v in candidates if isinstance(v, (int, float))), default=_DEFAULT_TIMEOUT_SECONDS)


class DevinACPClient:
    """Minimal OpenAI-client-compatible facade for Devin ACP."""

    # Already a complete client — never re-dispatched through wire adapter.
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None, default_headers: dict[str, str] | None = None,
        acp_command: str | None = None, acp_args: list[str] | None = None, acp_cwd: str | None = None,
        command: str | None = None, args: list[str] | None = None, **_: Any,
    ):
        self.api_key, self.base_url = api_key or "devin", base_url or ACP_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._acp_command = acp_command or command or _resolve_command()
        self._acp_args = list(acp_args or args or _resolve_args())
        self._acp_cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))
        self.is_closed, self._active_process = False, None
        self._active_process_lock = threading.Lock()

    def close(self) -> None:
        with self._active_process_lock:
            proc, self._active_process = self._active_process, None
        self.is_closed = True
        try:
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    def _create_chat_completion(
        self, *, model: str | None = None, messages: list[dict[str, Any]] | None = None, timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None, tool_choice: Any = None, stream: bool = False, **_: Any,
    ) -> Any:
        prompt_text = _format_messages_as_prompt(messages or [], model=model, tools=tools, tool_choice=tool_choice)
        response_text, reasoning = self._run_prompt(prompt_text, timeout_seconds=_effective_timeout(timeout), model=model)
        tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
        message = SimpleNamespace(
            content=cleaned_text, tool_calls=tool_calls, reasoning=reasoning or None, reasoning_content=reasoning or None,
            reasoning_details=None,
        )
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0, prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
            model=model or "devin",
        )
        return _completion_to_stream_chunks(completion) if stream else completion

    def _spawn(self) -> subprocess.Popen[str]:
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags
            proc = subprocess.Popen(
                [self._acp_command] + self._acp_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace', bufsize=1, cwd=self._acp_cwd, env=_build_subprocess_env(),
                creationflags=windows_hide_flags(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start Devin ACP command '{self._acp_command}'. "
                "Install Devin CLI (https://devin.ai) or set HERMES_DEVIN_ACP_COMMAND."
            ) from exc
        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError("Devin ACP process did not expose stdin/stdout pipes.")
        self.is_closed = False
        with self._active_process_lock:
            self._active_process = proc
        return proc

    @contextlib.contextmanager
    def _session(self, timeout_seconds: float) -> Iterator[tuple[dict[str, Any], Callable[..., Any]]]:
        """One ACP process: initialize → session/new. Yields (session, request_fn)."""
        proc = self._spawn()
        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=40)

        def _decode(line: str) -> dict[str, Any]:
            try:
                return json.loads(line)
            except Exception:
                return {"raw": line.rstrip("\n")}

        def _pump(stream, sink) -> None:
            for line in stream or ():
                sink(line)

        threading.Thread(target=_pump, args=(proc.stdout, lambda line: inbox.put(_decode(line))), daemon=True).start()
        threading.Thread(target=_pump, args=(proc.stderr, lambda line: stderr_tail.append(line.rstrip("\n"))), daemon=True).start()
        request_ids = iter(range(1, 1 << 62))
        session_deadline = time.monotonic() + timeout_seconds

        def _request(method: str, params: dict[str, Any], *, text_parts: list[str] | None = None,
                     reasoning_parts: list[str] | None = None) -> Any:
            request_id = next(request_ids)
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            while time.monotonic() < session_deadline and proc.poll() is None:
                try:
                    msg = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._handle_server_message(
                    msg, process=proc, cwd=self._acp_cwd, text_parts=text_parts, reasoning_parts=reasoning_parts,
                ) or msg.get("id") != request_id:
                    continue
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise RuntimeError(f"Devin ACP {method} failed: {err.get('message') or err}")
                return msg.get("result")
            stderr_text = "\n".join(stderr_tail).strip()
            if proc.poll() is not None and stderr_text:
                raise RuntimeError(f"Devin ACP process exited early: {stderr_text[:500]}")
            raise TimeoutError(f"Timed out waiting for Devin ACP response to {method}.")

        try:
            _request("initialize", _INITIALIZE_PARAMS)
            session = _request("session/new", {"cwd": self._acp_cwd, "mcpServers": []}) or {}
            if not str(session.get("sessionId") or "").strip():
                raise RuntimeError("Devin ACP did not return a sessionId.")
            yield session, _request
        finally:
            self.close()

    def list_models(self, *, timeout_seconds: float = 15.0) -> list[str]:
        """Open a fresh ACP session, return enabled model ids, close."""
        with self._session(timeout_seconds) as (session, _request_unused):
            return _session_model_ids(session)

    def _run_prompt(self, prompt_text: str, *, timeout_seconds: float, model: str | None = None) -> tuple[str, str]:
        requested_model = str(model or "").strip()
        with self._session(timeout_seconds) as (session, _request):
            session_id = str(session.get("sessionId") or "").strip()
            if requested_model and requested_model != "devin":
                try:
                    if (selection := _model_selection_request(session, requested_model)) is not None:
                        _request(*selection)
                    else:
                        logger.warning("Devin ACP does not offer model %r; using the session default.", requested_model)
                except Exception as exc:
                    logger.warning("Devin ACP model selection for %r failed; continuing with session default: %s", requested_model, exc)
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt_text}]}
            _request("session/prompt", prompt, text_parts=text_parts, reasoning_parts=reasoning_parts)
            return "".join(text_parts), "".join(reasoning_parts)

    def _handle_server_message(
        self, msg: dict[str, Any], *, process: subprocess.Popen[str], cwd: str,
        text_parts: list[str] | None, reasoning_parts: list[str] | None,
    ) -> bool:
        """Consume one server->client message. True when handled."""
        method = msg.get("method")
        if not isinstance(method, str):
            return False
        if method == "session/update":
            update = (msg.get("params") or {}).get("update") or {}
            content = update.get("content") or {}
            chunk_text = str(content.get("text") or "") if isinstance(content, dict) else ""
            sinks = {"agent_message_chunk": text_parts, "agent_thought_chunk": reasoning_parts}
            if chunk_text and (sink := sinks.get(str(update.get("sessionUpdate") or "").strip())) is not None:
                sink.append(chunk_text)
            return True
        # Notifications we don't act on (mcp/output/etc) — still handled.
        if method.startswith("_") or "/" in method and msg.get("id") is None:
            return True
        return False



