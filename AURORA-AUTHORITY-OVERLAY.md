# Aurora Explicit-Owner Authority Overlay

This branch carries Aurora's L0 integration for the Mnemosyne explicit-owner authority fork.

## Invariants

1. Only authenticated/allow-listed owner messages can author explicit owner directives.
2. Capture occurs from the clean inbound user message before memory nudges, review, augmentation, or model transformation.
3. The newest relevant explicit owner directive is injected into the active system prompt only after compression settles. It does not modify the persisted clean user message, cached base prompt, or plugin user context.
4. Lower-authority `memory` and `skill_manage` writes, replacements, removals, and staged replays are authority-gated before filesystem mutation.
5. Background review is not trusted merely because it is internal; its memory and skill tools traverse the same gates.
6. Structured authority rejection is a completed safety result, not a generic tool crash.
7. The gateway must not be restarted merely to test source changes. Activation occurs once, after source, package, database-runtime, watchdog, and rollback gates pass.
8. Busy-turn control plane (2026-07-25): an authenticated owner text (identity in the platform allowlist via `is_explicit_owner_source`) supersedes steer mode, queue mode, active-subagent protection, and compression-in-flight protection. It becomes the next real queued user turn and calls `running_agent.interrupt(text)` to terminate stale parent/tool/child work. Guest traffic keeps normal steer/queue/demotion behavior.
9. Broad-suite red remains red regardless of attribution. The 2026-07-25 baseline failure board was repaired through four bounded Sol lanes, parent-integrated, and proven at 1,862/1,862 on the previously failing 59-file set; the complete agent+gateway rerun is the release gate.
10. Durable deployment authority is `Dockerfile.aurora`: pinned linux/amd64 official bases, SQLite 3.51.3 fixed branch, promoted Mnemosyne wheel hashes, faster-whisper 1.2.1, fail-closed startup identity, exact `hermes gateway`, container restart policy, and no embedded secrets. `docker/aurora-deploy-runbook.sh` owns host activation/rollback. Userspace venv is a bridge, not the end-state architecture.

## Custom image contract

- The effective build context is a clean `git archive HEAD`, not the live checkout. Ignored/untracked proof, auth, database, and secret files cannot enter the image context.
- The upstream base is tagged by full commit SHA and built for `linux/amd64` from digest-pinned uv, Node, and Debian bases. The overlay must receive and match the same SHA through `AURORA_AUTHORITY_SHA`.
- SQLite 3.51.3 is compiled inside the image from the vendored, SHA-256-verified `sqlite-autoconf-3510300.tar.gz`; host-built shared libraries are not accepted as rebuild authority.
- Mnemosyne 3.14.2 / provider 0.5.2 install from vendored, SHA-256-verified wheels through `uv pip`; the stock venv intentionally has no `pip` module.
- Local STT is `faster-whisper==1.2.1`; the release gate must transcribe a real cached audio file with model `small`, CPU, and `int8` on the exact built image.
- Startup fails closed unless SQLite, STT, Mnemosyne, build SHA, owner-authority import, gateway control-plane wiring, credential non-embedding, and exact `hermes gateway` command checks pass.
- Health uses exact `/proc` argv + non-zombie matching through `docker/aurora-gateway-healthcheck.py`; `pgrep -f` is forbidden because it can match its own healthcheck command.
- Activation preserves the old container itself under a rollback name. On any live gate failure the new container is removed and the exact preserved container is renamed and started. Runtime flags are transported as NUL-separated argv; secret environment values are never serialized and come only from the approved `/opt/data/.env` path.

## Activation prerequisite gate (2026-07-25)

| Required condition | State | Evidence / consequence |
|---|---|---|
| Source and targeted tests pass | **known-true** | Local fixed-source test receipts under `.hermes/proof/owner-control-plane/` |
| Vendored wheel/source hashes match | **known-true** | `scripts/validate-aurora-image-context.py` |
| Local SQLite 3.51.3 + real cached-audio STT work | **known-true** | Direct local runtime proof; this does not substitute for exact-image proof |
| Docker daemon reachable from this container | **known-false** | No Docker socket/daemon access in the current runtime |
| Exact custom image built and offline-gated | **known-false** | Cannot occur until host Docker access exists |
| Live container runtime inspect satisfies the runbook | **unknown** | Must be machine-checked on the host immediately before build |
| Owner explicitly approves the one controlled activation | **known-false** | No activation approval has been given |

Because required conditions are known-false, **activation is INVALID in the current session**. Do not imply the image is deployed or tested. The host-side runbook is staged for a later, owner-approved activation only.

## Dynamic source authority

- Clean owner capture and response prompt: `agent/owner_directive_capture.py`
- Turn integration: `agent/turn_context.py`
- Legacy memory/background review gate: `tools/memory_tool.py`
- Skill mutation gate: `tools/skill_manager_tool.py`
- Custom image preflight: `scripts/validate-aurora-image-context.py`
- Structured runtime replay: `scripts/docker-inspect-runtime-argv.py`
- Host activation/exact rollback: `docker/aurora-deploy-runbook.sh`
- Project map: `PROJECT-MAP.json`

Tests live under the corresponding `tests/agent/` and `tests/tools/` modules. The complete source, manifest, installed-package, dependency, proposal-isolation, diagnostics, and NLI gate is `scripts/aurora-authority-regression.sh`. Mnemosyne core/provider behavior and release artifacts are authoritative in `/opt/data/mnemosyne/fork-src/PROJECT-MAP.json`.
