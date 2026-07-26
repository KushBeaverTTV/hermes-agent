#!/bin/sh
# Aurora custom-image startup identity check.
# Runs inside the container (cont-init or manual). Fails loudly if the image
# does not carry the expected repaired source, fixed sqlite, fork wheels, STT.
set -eu

PY=/opt/hermes/.venv/bin/python3
EXPECTED_GATEWAY_SOURCE=/opt/hermes/gateway/run.py
fail() { echo "AURORA-STARTUP-FAIL: $*" >&2; exit 1; }

# 0. Base Hermes project remains installed after Aurora overlay dependency sync.
[ -x /opt/hermes/.venv/bin/hermes ] || fail "Hermes CLI missing from image venv"
"$PY" -c "import hermes_cli, hermes_constants" 2>/dev/null \
  || fail "Hermes project modules missing from image venv"
echo "AURORA-STARTUP: Hermes CLI/project install OK"

# 1. Fixed sqlite branch
"$PY" - <<'PY' || fail "sqlite not fixed branch"
import sqlite3
v = sqlite3.sqlite_version
maj, minr, patch = map(int, v.split('.'))
ok = ((maj == 3 and minr == 44 and patch >= 6)
      or (maj == 3 and minr == 50 and patch >= 7)
      or (maj == 3 and minr == 51 and patch >= 3)
      or (maj > 3 or (maj == 3 and minr > 51)))
assert ok, f"vulnerable sqlite {v}"
print(f"AURORA-STARTUP: sqlite {v} OK")
PY

# 2. faster-whisper present
"$PY" -c "import faster_whisper" 2>/dev/null || fail "faster-whisper missing"
echo "AURORA-STARTUP: faster-whisper OK"

# 3. Mnemosyne fork versions
"$PY" - <<'PY' || fail "mnemosyne fork mismatch"
import importlib.metadata as md
assert md.version("mnemosyne-memory") == "3.14.2", md.version("mnemosyne-memory")
assert md.version("mnemosyne-hermes") == "0.5.2", md.version("mnemosyne-hermes")
print("AURORA-STARTUP: mnemosyne core=3.14.2 provider=0.5.2 OK")
PY

"$PY" /opt/aurora/vendor-context/scripts/verify-aurora-vendor-manifest.py \
  --root /opt/aurora/vendor-context \
  || fail "vendor manifest mismatch"

# 4. Owner authority source and immutable build identity present
[ -s /opt/hermes/.hermes_build_sha ] || fail "base build SHA missing"
[ -s /opt/aurora-authority-sha ] || fail "authority build SHA missing"
cmp -s /opt/hermes/.hermes_build_sha /opt/aurora-authority-sha \
  || fail "base/authority SHA mismatch"
"$PY" -c "from agent.owner_directive_capture import is_explicit_owner_source" 2>/dev/null \
  || fail "owner authority capture missing"
ACTUAL_GATEWAY_SOURCE="$($PY -c 'import pathlib, gateway.run; print(pathlib.Path(gateway.run.__file__).resolve())')" \
  || fail "gateway source import failed"
[ "$ACTUAL_GATEWAY_SOURCE" = "$EXPECTED_GATEWAY_SOURCE" ] \
  || fail "gateway source mismatch: $ACTUAL_GATEWAY_SOURCE"
"$PY" - <<'PY' || fail "owner gateway control-plane wiring missing"
import inspect

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.run import GatewayRunner

assert issubclass(GatewayRunner, GatewayAuthorizationMixin)
owner_source = inspect.getsource(GatewayAuthorizationMixin._is_explicit_owner_source)
owner_event = inspect.getsource(GatewayRunner._is_explicit_owner_event)
assert "owner_user_ids" in owner_source
assert 'allowed = _coerce_allow_set(raw_owner_ids) - {"*"}' in owner_source
assert "self._is_explicit_owner_source" in owner_event
print("AURORA-STARTUP: owner gateway control-plane wiring OK")
PY
echo "AURORA-STARTUP: owner authority SHA $(cat /opt/aurora-authority-sha) OK"

# 5. No embedded secrets (image must not bake tokens)
for f in /opt/hermes/.env /opt/hermes/auth.json; do
  [ -e "$f" ] && fail "embedded secret file present: $f"
done
echo "AURORA-STARTUP: no embedded secrets OK"

echo "AURORA STARTUP CHECK PASS"
