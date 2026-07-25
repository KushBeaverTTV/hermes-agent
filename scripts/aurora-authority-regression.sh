#!/usr/bin/env bash
# Full source and installed-runtime regression gate for Aurora explicit-owner authority.
set -euo pipefail

ROOT=/opt/data
HERMES="$ROOT/hermes-agent"
MNEMO="$ROOT/mnemosyne/fork-src"
PYTHON="$ROOT/venvs/hermes/bin/python"
VALIDATOR="$ROOT/skills/software-development/agent-layers-closeout/scripts/validate_project_integrity.py"
SOURCE_PATH="$MNEMO/mnemosyne-memory:$MNEMO/mnemosyne-hermes/src:$HERMES"
LIVE_MNEMO="$ROOT/venvs/mnemosyne/lib/python3.13/site-packages"

"$PYTHON" "$VALIDATOR" --manifest "$MNEMO/PROJECT-MAP.json"
"$PYTHON" "$VALIDATOR" --manifest "$HERMES/PROJECT-MAP.json"

PYTHONPATH="$SOURCE_PATH" "$PYTHON" -m pytest -q \
  "$MNEMO/mnemosyne-memory/tests/test_authority.py" \
  "$MNEMO/mnemosyne-memory/tests/test_authority_core_writes.py" \
  "$MNEMO/mnemosyne-hermes/tests/test_authority_gate.py" \
  "$MNEMO/mnemosyne-hermes/tests/test_prefetch_hardening.py" \
  "$HERMES/tests/agent/test_owner_directive_capture.py" \
  "$HERMES/tests/agent/test_turn_context.py" \
  "$HERMES/tests/tools/test_memory_tool.py" \
  "$HERMES/tests/tools/test_skill_manager_tool.py"

PYTHONPATH="$LIVE_MNEMO" "$PYTHON" - <<'PY'
import importlib.metadata, mnemosyne, mnemosyne_hermes
assert mnemosyne.__version__ == importlib.metadata.version("mnemosyne-memory")
assert mnemosyne_hermes.__version__ == importlib.metadata.version("mnemosyne-hermes")
print(f"PASS: installed identity core={mnemosyne.__version__} provider={mnemosyne_hermes.__version__}")
PY
env -u PYTHONPATH "$ROOT/venvs/mnemosyne/bin/python" "$MNEMO/scripts/test_internal_proposal_recall.py"
env -u PYTHONPATH "$ROOT/venvs/mnemosyne/bin/python" "$MNEMO/scripts/test_recall_diagnostics_config.py"
env -u PYTHONPATH "$ROOT/venvs/mnemosyne/bin/python" "$MNEMO/scripts/test_authority_nli_runtime.py"
env -u PYTHONPATH "$ROOT/venvs/mnemosyne/bin/python" -m pip check
printf '%s\n' 'PASS: Aurora explicit-owner authority regression gate'
