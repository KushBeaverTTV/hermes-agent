#!/usr/bin/env bash
# Aurora custom image — host-side build/deploy/exact-container rollback.
# Run on the Hostinger HOST, never inside the live container.
set -euo pipefail

ROOT=/opt/data/hermes-agent
CONTAINER="${CONTAINER:-hermes}"
PLATFORM=linux/amd64
RECEIPT_DIR=/opt/data/aurora-image-rollback
cd "$ROOT"

rollback_saved_container() {
    local saved
    [[ -r "$RECEIPT_DIR/container-name.txt" ]] || {
        echo "ROLLBACK FAIL: missing $RECEIPT_DIR/container-name.txt" >&2
        return 1
    }
    IFS= read -r saved < "$RECEIPT_DIR/container-name.txt"
    [[ -n "$saved" ]] || { echo "ROLLBACK FAIL: empty saved container name" >&2; return 1; }
    docker inspect "$saved" >/dev/null
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker rename "$saved" "$CONTAINER"
    docker start "$CONTAINER"
    echo "ROLLBACK PASS: restored exact preserved container $CONTAINER"
}

if [[ "${1:-}" == "--rollback" ]]; then
    rollback_saved_container
    exit 0
fi

# ---------- 0. Preconditions: all must be known true ----------
command -v docker >/dev/null || { echo "INVALID: docker client missing" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "INVALID: Docker daemon unreachable" >&2; exit 1; }
[[ "$(uname -m)" == "x86_64" ]] || { echo "INVALID: host is not linux/amd64" >&2; exit 1; }
[[ -r /opt/data/.env ]] || { echo "INVALID: approved env-file missing" >&2; exit 1; }
docker inspect "$CONTAINER" >/dev/null || { echo "INVALID: live container $CONTAINER missing" >&2; exit 1; }

DIRTY="$(git status --porcelain=v1 --untracked-files=all)"
[[ -z "$DIRTY" ]] || {
    echo "INVALID: source tree is dirty; commit the exact tested tree before image build" >&2
    printf '%s\n' "$DIRTY" >&2
    exit 1
}

python3 scripts/validate-aurora-image-context.py
GIT_SHA="$(git rev-parse HEAD)"
BASE_IMAGE="hermes-upstream:${GIT_SHA}"
IMAGE="aurora-hermes:3.6.0-${GIT_SHA:0:12}"
ROLLBACK_CONTAINER="${CONTAINER}-rollback-${GIT_SHA:0:12}"
CURRENT_IMAGE="$(docker inspect --format='{{.Image}}' "$CONTAINER")"

docker inspect "$ROLLBACK_CONTAINER" >/dev/null 2>&1 && {
    echo "INVALID: rollback container already exists: $ROLLBACK_CONTAINER" >&2
    exit 1
}
mkdir -p "$RECEIPT_DIR"
printf '%s\n' "$CURRENT_IMAGE" > "$RECEIPT_DIR/image-id.txt"
printf '%s\n' "$ROLLBACK_CONTAINER" > "$RECEIPT_DIR/container-name.txt"
printf '%s\n' "$GIT_SHA" > "$RECEIPT_DIR/authority-sha.txt"

# Preserve non-secret runtime settings as structured NUL argv. Environment
# values are intentionally never serialized; the approved env-file is reused.
ARGV_FILE="$(mktemp)"
docker inspect "$CONTAINER" \
  | python3 scripts/docker-inspect-runtime-argv.py --format nul > "$ARGV_FILE"
mapfile -d '' -t RUNTIME_ARGS < "$ARGV_FILE"
rm -f "$ARGV_FILE"
((${#RUNTIME_ARGS[@]} > 0)) || { echo "INVALID: no runtime argv generated" >&2; exit 1; }
HAS_DATA_BIND=0
for arg in "${RUNTIME_ARGS[@]}"; do
    [[ "$arg" == /opt/data:/opt/data* ]] && HAS_DATA_BIND=1
done
((HAS_DATA_BIND == 1)) || { echo "INVALID: /opt/data bind not present in live runtime" >&2; exit 1; }
printf 'RUNTIME ARGV PASS count=%d sample=' "${#RUNTIME_ARGS[@]}"
printf ' %q' "${RUNTIME_ARGS[@]:0:8}"
printf '\n'

# Build from committed files only. Ignored/untracked proof, database, auth, and
# secret files never enter the effective Docker context.
CONTEXT_DIR="$(mktemp -d)"
cleanup_context() { rm -rf "$CONTEXT_DIR"; }
trap cleanup_context EXIT
git archive --format=tar HEAD | tar -xf - -C "$CONTEXT_DIR"

# ---------- 1. Build pinned upstream base ----------
docker build --platform "$PLATFORM" --build-arg HERMES_GIT_SHA="$GIT_SHA" \
  -t "$BASE_IMAGE" -f "$CONTEXT_DIR/Dockerfile" "$CONTEXT_DIR"

# ---------- 2. Build Aurora overlay ----------
docker build --platform "$PLATFORM" --build-arg AURORA_BASE="$BASE_IMAGE" \
  --build-arg AURORA_AUTHORITY_SHA="$GIT_SHA" \
  -t "$IMAGE" -f "$CONTEXT_DIR/Dockerfile.aurora" "$CONTEXT_DIR"
cleanup_context
trap - EXIT

# ---------- 3. Offline gates on the exact built image ----------
docker run --rm --platform "$PLATFORM" \
  --entrypoint /opt/aurora/startup-check.sh "$IMAGE"
docker run --rm --platform "$PLATFORM" -v /opt/data:/opt/data \
  --entrypoint /opt/hermes/.venv/bin/python3 "$IMAGE" -c \
  "from faster_whisper import WhisperModel; m=WhisperModel('small', device='cpu', compute_type='int8'); \
import glob,os; fs=glob.glob('/opt/data/cache/audio/*.ogg'); assert fs, 'no cached audio'; \
f=max(fs,key=os.path.getmtime); segs,_=m.transcribe(f); text=' '.join(s.text for s in segs).strip(); \
assert text, 'empty transcription'; print('STT OK', f, text[:80])"

# ---------- 4. One controlled activation with automatic exact rollback ----------
ROLLBACK_READY=0
rollback_on_error() {
    local rc=$?
    trap - ERR
    if ((ROLLBACK_READY == 1)); then
        echo "ACTIVATION FAIL: restoring exact preserved container" >&2
        rollback_saved_container || true
    fi
    exit "$rc"
}
trap rollback_on_error ERR

docker stop "$CONTAINER"
docker rename "$CONTAINER" "$ROLLBACK_CONTAINER"
ROLLBACK_READY=1

docker run -d --name "$CONTAINER" --platform "$PLATFORM" \
  "${RUNTIME_ARGS[@]}" --env-file /opt/data/.env "$IMAGE"

# ---------- 5. Live gates ----------
HEALTH=starting
for _ in {1..24}; do
    HEALTH="$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER")"
    [[ "$HEALTH" == healthy ]] && break
    [[ "$HEALTH" == unhealthy ]] && { docker logs --tail 100 "$CONTAINER" >&2; false; }
    sleep 5
done
[[ "$HEALTH" == healthy ]] || { echo "health timeout: $HEALTH" >&2; false; }

docker exec "$CONTAINER" /opt/aurora/startup-check.sh
docker exec "$CONTAINER" /opt/hermes/.venv/bin/python3 -c \
  "import sqlite3; assert sqlite3.sqlite_version == '3.51.3'; print('live sqlite', sqlite3.sqlite_version)"
docker logs --tail 200 "$CONTAINER" | grep -Eiq 'telegram.*(connect|poll|start|ready)'

trap - ERR
ROLLBACK_READY=0
echo "ACTIVATION PASS image=$IMAGE health=$HEALTH"
echo "Exact rollback container retained: $ROLLBACK_CONTAINER"
echo "Rollback command: $0 --rollback"
