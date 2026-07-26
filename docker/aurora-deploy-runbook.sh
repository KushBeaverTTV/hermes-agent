#!/usr/bin/env bash
# Aurora custom image — plan, prepare, approval-gated cutover, exact rollback.
# Run on the Hostinger HOST, never inside the live container.
set -euo pipefail

ROOT="${AURORA_SOURCE_ROOT:-/opt/data/hermes-agent}"
CONTAINER="${CONTAINER:-hermes}"
PLATFORM="${AURORA_PLATFORM:-linux/amd64}"
RECEIPT_ROOT="${AURORA_RECEIPT_ROOT:-/opt/data/aurora-image-rollback}"
MODE="${AURORA_DEPLOY_MODE:-plan}"
HOST_DATA_DIR="${AURORA_HOST_DATA_DIR:-}"
ENV_FILE="${AURORA_ENV_FILE:-}"

case "${1:-}" in
  ""|--plan) MODE=plan ;;
  --prepare) MODE=prepare ;;
  --cutover) MODE=cutover ;;
  --rollback) MODE=rollback ;;
  *) echo "usage: $0 [--plan|--prepare|--cutover|--rollback [DEPLOYMENT_SHA]]" >&2; exit 2 ;;
esac

cd "$ROOT"
DIRTY="$(git status --porcelain=v1 --untracked-files=all)"
[[ -z "$DIRTY" ]] || {
  echo "INVALID: source tree is dirty; commit the exact tested tree first" >&2
  printf '%s\n' "$DIRTY" >&2
  exit 1
}
python3 scripts/validate-aurora-image-context.py --root "$ROOT"
GIT_SHA="$(git rev-parse HEAD)"
if [[ "$MODE" == rollback ]]; then
  DEPLOYMENT_SHA="${2:-${AURORA_ROLLBACK_SHA:-$GIT_SHA}}"
else
  DEPLOYMENT_SHA="$GIT_SHA"
fi
[[ "$DEPLOYMENT_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "INVALID: deployment SHA must be 40 lowercase hex characters" >&2
  exit 1
}
BASE_IMAGE="hermes-upstream:${GIT_SHA}"
IMAGE="aurora-hermes:${GIT_SHA}"
RECEIPT_DIR="$RECEIPT_ROOT/$DEPLOYMENT_SHA"
ROLLBACK_CONTAINER="${CONTAINER}-rollback-${DEPLOYMENT_SHA:0:12}"
AURORA_CUTOVER_APPROVED="${AURORA_CUTOVER_APPROVED:-}"
AURORA_ROLLBACK_APPROVED="${AURORA_ROLLBACK_APPROVED:-}"

make_context() {
  CONTEXT_DIR="$(mktemp -d)"
  git archive --format=tar HEAD | tar -xf - -C "$CONTEXT_DIR"
  python3 "$CONTEXT_DIR/scripts/validate-aurora-image-context.py" --root "$CONTEXT_DIR"
}

cleanup_context() {
  [[ -n "${CONTEXT_DIR:-}" ]] && rm -rf "$CONTEXT_DIR"
}

require_docker() {
  command -v docker >/dev/null || { echo "INVALID: docker client missing" >&2; return 1; }
  docker info >/dev/null 2>&1 || { echo "INVALID: Docker daemon unreachable" >&2; return 1; }
  [[ "$(uname -m)" == "x86_64" ]] || { echo "INVALID: host is not linux/amd64" >&2; return 1; }
}

resolve_host_paths() {
  local -a data_sources=()
  if [[ -z "$HOST_DATA_DIR" ]]; then
    mapfile -t data_sources < <(
      docker inspect "$CONTAINER" | python3 -c '
import json
import sys

objects = json.load(sys.stdin)
if len(objects) != 1:
    raise SystemExit(f"expected one container inspection, got {len(objects)}")
for mount in objects[0].get("Mounts") or []:
    if mount.get("Destination") == "/opt/data":
        print(mount.get("Source") or "")
'
    )
    ((${#data_sources[@]} == 1)) || {
      echo "INVALID: expected exactly one host source mounted at /opt/data" >&2
      return 1
    }
    HOST_DATA_DIR="${data_sources[0]}"
  fi
  [[ -d "$HOST_DATA_DIR" ]] || { echo "INVALID: host data directory missing" >&2; return 1; }
  ENV_FILE="${ENV_FILE:-$HOST_DATA_DIR/.env}"
  [[ -r "$ENV_FILE" ]] || { echo "INVALID: approved env-file missing" >&2; return 1; }
  echo "HOST PATHS PASS data=$HOST_DATA_DIR env=<approved>"
}

capture_runtime_argv() {
  local argv_file
  docker inspect "$CONTAINER" >/dev/null
  argv_file="$(mktemp)"
  docker inspect "$CONTAINER" \
    | python3 scripts/docker-inspect-runtime-argv.py --format nul > "$argv_file"
  mapfile -d '' -t RUNTIME_ARGS < "$argv_file"
  rm -f "$argv_file"
  ((${#RUNTIME_ARGS[@]} > 0)) || { echo "INVALID: no runtime argv generated" >&2; return 1; }
  local has_data_bind=0 arg
  for arg in "${RUNTIME_ARGS[@]}"; do
    [[ "$arg" == *:/opt/data || "$arg" == *:/opt/data:* ]] && has_data_bind=1
  done
  ((has_data_bind == 1)) || { echo "INVALID: /opt/data bind absent" >&2; return 1; }
  printf 'RUNTIME ARGV PASS count=%d sample=' "${#RUNTIME_ARGS[@]}"
  printf ' %q' "${RUNTIME_ARGS[@]:0:8}"
  printf '\n'
}

rollback_saved_container() {
  local preserved_image
  docker inspect "$ROLLBACK_CONTAINER" >/dev/null
  preserved_image="$(docker inspect --format='{{.Image}}' "$ROLLBACK_CONTAINER")"
  python3 scripts/validate-aurora-rollback-receipt.py \
    --receipt-dir "$RECEIPT_DIR" \
    --deployment-sha "$DEPLOYMENT_SHA" \
    --expected-container "$ROLLBACK_CONTAINER" \
    --preserved-image "$preserved_image"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker rename "$ROLLBACK_CONTAINER" "$CONTAINER"
  docker start "$CONTAINER"
  echo "ROLLBACK PASS: restored exact preserved container $CONTAINER"
}

case "$MODE" in
  plan)
    make_context
    cleanup_context
    echo "PLAN source=$GIT_SHA base=$BASE_IMAGE image=$IMAGE platform=$PLATFORM"
    echo "PLAN PASS: no build, stop, rename, run, restart, or cutover performed"
    ;;

  prepare)
    require_docker
    resolve_host_paths
    capture_runtime_argv
    mkdir -p "$RECEIPT_DIR"
    make_context
    trap cleanup_context EXIT
    BUILD_CREATED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    docker build --platform "$PLATFORM" --build-arg HERMES_GIT_SHA="$GIT_SHA" \
      -t "$BASE_IMAGE" -f "$CONTEXT_DIR/Dockerfile" "$CONTEXT_DIR"
    docker build --platform "$PLATFORM" --build-arg AURORA_BASE="$BASE_IMAGE" \
      --build-arg AURORA_AUTHORITY_SHA="$GIT_SHA" \
      --build-arg AURORA_BUILD_CREATED="$BUILD_CREATED" \
      -t "$IMAGE" -f "$CONTEXT_DIR/Dockerfile.aurora" "$CONTEXT_DIR"
    cleanup_context
    trap - EXIT

    docker run --rm --platform "$PLATFORM" \
      --entrypoint /opt/aurora/startup-check.sh "$IMAGE"
    [[ -d "$HOST_DATA_DIR/cache/audio" ]] || { echo "INVALID: cached audio directory missing" >&2; exit 1; }
    mkdir -p "$HOST_DATA_DIR/cache/huggingface"
    docker run --rm --platform "$PLATFORM" \
      -v "$HOST_DATA_DIR/cache/audio:/opt/data/cache/audio:ro" \
      -v "$HOST_DATA_DIR/cache/huggingface:/opt/data/cache/huggingface:rw" \
      --entrypoint /opt/hermes/.venv/bin/python3 "$IMAGE" -c \
      "from faster_whisper import WhisperModel; import glob,os; m=WhisperModel('small', device='cpu', compute_type='int8'); fs=glob.glob('/opt/data/cache/audio/*.ogg'); assert fs, 'no cached audio'; f=max(fs,key=os.path.getmtime); segs,_=m.transcribe(f); text=' '.join(s.text for s in segs).strip(); assert text, 'empty transcription'; print('STT OK chars', len(text))"

    docker image inspect --format='{{.Id}}' "$IMAGE" > "$RECEIPT_DIR/image-id.txt"
    printf '%s\n' "$GIT_SHA" > "$RECEIPT_DIR/authority-sha.txt"
    printf '%s\n' "$ROLLBACK_CONTAINER" > "$RECEIPT_DIR/container-name.txt"
    echo "PREPARE PASS image=$IMAGE receipt=$RECEIPT_DIR (live container untouched)"
    ;;

  cutover)
    [[ "$AURORA_CUTOVER_APPROVED" == "YES:$GIT_SHA" ]] || {
      echo "INVALID: cutover requires AURORA_CUTOVER_APPROVED=YES:$GIT_SHA" >&2
      exit 1
    }
    require_docker
    resolve_host_paths
    [[ -r "$RECEIPT_DIR/authority-sha.txt" && -r "$RECEIPT_DIR/image-id.txt" ]] || {
      echo "INVALID: exact prepare receipts missing" >&2
      exit 1
    }
    [[ "$(<"$RECEIPT_DIR/authority-sha.txt")" == "$GIT_SHA" ]] || {
      echo "INVALID: prepared authority SHA mismatch" >&2
      exit 1
    }
    [[ "$(docker image inspect --format='{{.Id}}' "$IMAGE")" == "$(<"$RECEIPT_DIR/image-id.txt")" ]] || {
      echo "INVALID: prepared image ID mismatch" >&2
      exit 1
    }
    capture_runtime_argv
    docker inspect "$ROLLBACK_CONTAINER" >/dev/null 2>&1 && {
      echo "INVALID: rollback container already exists: $ROLLBACK_CONTAINER" >&2
      exit 1
    }
    CURRENT_IMAGE="$(docker inspect --format='{{.Image}}' "$CONTAINER")"
    printf '%s\n' "$CURRENT_IMAGE" > "$RECEIPT_DIR/previous-image-id.txt"

    rollback_ready=0
    rollback_on_error() {
      local rc=$?
      trap - ERR
      if ((rollback_ready == 1)); then
        echo "CUTOVER FAIL: restoring exact preserved container" >&2
        rollback_saved_container || true
      fi
      exit "$rc"
    }
    trap rollback_on_error ERR

    docker stop "$CONTAINER"
    docker rename "$CONTAINER" "$ROLLBACK_CONTAINER"
    rollback_ready=1
    docker run -d --name "$CONTAINER" --platform "$PLATFORM" \
      "${RUNTIME_ARGS[@]}" --env-file "$ENV_FILE" "$IMAGE"

    health=starting
    for _ in {1..48}; do
      health="$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER")"
      [[ "$health" == healthy ]] && break
      [[ "$health" == unhealthy ]] && { docker logs --tail 100 "$CONTAINER" >&2; false; }
      sleep 5
    done
    [[ "$health" == healthy ]] || { echo "health timeout: $health" >&2; false; }
    docker exec "$CONTAINER" /opt/aurora/startup-check.sh
    trap - ERR
    rollback_ready=0
    echo "CUTOVER PASS image=$IMAGE health=$health"
    echo "Exact rollback container retained: $ROLLBACK_CONTAINER"
    echo "Rollback command: AURORA_ROLLBACK_APPROVED=YES:$GIT_SHA $0 --rollback $GIT_SHA"
    ;;

  rollback)
    [[ "$AURORA_ROLLBACK_APPROVED" == "YES:$DEPLOYMENT_SHA" ]] || {
      echo "INVALID: rollback requires AURORA_ROLLBACK_APPROVED=YES:$DEPLOYMENT_SHA" >&2
      exit 1
    }
    require_docker
    rollback_saved_container
    ;;

  *) echo "INVALID: unsupported mode $MODE" >&2; exit 2 ;;
esac
