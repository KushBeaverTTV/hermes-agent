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

## Dynamic source authority

- Clean owner capture and response prompt: `agent/owner_directive_capture.py`
- Turn integration: `agent/turn_context.py`
- Legacy memory/background review gate: `tools/memory_tool.py`
- Skill mutation gate: `tools/skill_manager_tool.py`
- Project map: `PROJECT-MAP.json`

Tests live under the corresponding `tests/agent/` and `tests/tools/` modules. Mnemosyne core/provider behavior and release artifacts are authoritative in `/opt/data/mnemosyne/fork-src/PROJECT-MAP.json`.
