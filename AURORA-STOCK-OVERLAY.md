# Aurora stock overlay

## Authority

This branch is a minimal Aurora/Kush overlay on the current authoritative `origin/main` Hermes source. It is **not a complete migration of prior Aurora custom work** and must not be described as feature-complete. Git commit ancestry is the sole source of truth for exact upstream and overlay identities; this document does not duplicate volatile SHAs.

Inspect identity with:

```bash
git rev-parse origin/main HEAD
git log --oneline --reverse origin/main..HEAD
```

## Retained upgrades

1. **Authenticated owner busy-turn supersession**
   - Exact primary `user_id` membership in the platform's dedicated `gateway.platforms.<platform>.owner_user_ids` config list grants owner control-plane authority; the loader stores it in platform `extra`, while alternate IDs, pairing approvals, wildcard access, environment variables, and conversation allowlists cannot bypass stock authorization.
   - Pairing-mirrored conversation allowlists are never used as the owner authority source, so approving a pairing code cannot silently grant control-plane priority.
   - A non-empty external owner text message becomes the next standalone turn and interrupts stale active work instead of being steered, queued behind stale work, media-merged, dropped at the queue cap, or demoted behind subagent/compression protection. Internal/system-generated events never gain owner authority. Existing pending work moves behind the owner turn in FIFO order; when the 32-turn cap is already full, the stale tail is evicted instead of the owner instruction.
   - During the short agent-startup sentinel window, the owner turn still becomes the standalone queue head but does not attempt an interrupt because no real agent exists yet.
   - During drain, the owner turn is queued by the dedicated preemption path (not the stale merge path) so it becomes the standalone head and stale media/text moves behind it. Drain-disabled rejects owner text like any other message.
   - Pairing, roles, allow-all flags, chat allowlists, bots, wildcard `*`, display names, chat names, and other user-controlled labels do not grant this authority.
   - The same rule is enforced in both stock busy-message entry paths: the adapter callback and the direct `_handle_message` PRIORITY block. It does not alter prompts, memory, or the agent loop.

2. **Project integrity manifest**
   - `PROJECT-MAP.json` registers this branch's active authority and external archive root.
   - Test transcripts and campaign receipts stay outside the project under `/opt/data/archives/hermes-stock-rescue/`.

## Migration status and preserved backlog

The previous customized source remains preserved at `/opt/data/hermes-agent`; it has not been deleted, reset, or cleaned. Its three custom commits, unfinished tracked changes, and untracked build/deployment files are inventoried and backed up in `/opt/data/archives/hermes-stock-rescue/2026-07-25/CUSTOM-WORK-RECOVERY-LEDGER.md` with a machine-readable hash manifest and restorable bundle/patch/archive files beside it.

Only the busy-turn owner-priority behavior has been rebuilt in this clean candidate. Every other prior feature or unfinished change remains **pending review**. Omitting an old implementation from this branch does not abandon the underlying product requirement and is not approval to delete its source.

## Not yet carried into the clean candidate

The previous fork's `agent.owner_directive_capture` subsystem is not yet replayed. Its current implementation uses dynamic per-turn system-prompt injection, a directive journal, an LLM contradiction classifier, and memory/skill mutation hooks that couple stock Hermes to an external Mnemosyne authority module and conflict with the stock prompt-cache boundary. The implementation requires redesign and review; the explicit-owner authority requirement remains active.

The partial mixed-timestamp insights patch is not yet replayed. Hostile review proved that it fixed overview/top-session math but left activity generation and SQL windowing inconsistent. Its source and tests remain preserved for completion or an explicit rejection; shipping the incomplete patch would be less trustworthy than stock.

The previous custom Docker/release campaign is also not part of this source overlay. Its Dockerfile, pinned runtime inputs, startup/health checks, activation/rollback runbook, and tests remain preserved as unfinished work. Runtime activation is a separate controlled operation and must not be confused with source correctness.

## Required gates

Before activation or merge:

1. Project integrity validator reports PASS using `/opt/data/skills/software-development/agent-layers-closeout/scripts/validate_project_integrity.py --manifest PROJECT-MAP.json`.
2. The complete focused command `python -m pytest tests/gateway/test_subagent_protection_30170.py tests/gateway/test_priority_path_compression_demotion_56391.py` is green. Auditable contracts include `test_exact_allowlist_owner_supersedes_steer_and_subagent_demotion`, `test_secondary_profile_never_inherits_default_owner_ids`, `test_exact_owner_priority_path_bypasses_subagent_and_compression_demotion`, both drain tests, startup-sentinel coverage, media exclusion, pairing isolation, alternate-ID rejection, and the direct `_handle_message` PRIORITY tests.
3. Adjacent authorization, busy, and interrupt tests are green on an existence-validated argv selection; the authoritative command and output are retained under the external receipt root declared in `PROJECT-MAP.json`.
4. Broad `tests/agent tests/gateway` is compared against immutable stock base `78c06525e8e955e06a007b07b347c679f3977c3e` on the identical selection. A red stock baseline remains red; only a verified zero-new-red delta may be described as no regression.
5. Independent read-only review findings are corrected or explicitly blocked with evidence.
6. Live gateway is not restarted or reconfigured as a side effect of source testing.

## Runtime boundary

Source proof does not imply live activation. Activation must preserve the Telegram lifeline, use a tested rollback, and verify the resulting running source identity. If the host/container cannot perform that safely, source stays committed and activation is reported blocked rather than implied.
