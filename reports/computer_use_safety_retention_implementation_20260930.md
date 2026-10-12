# Computer-use safety retention: implementation and validation

Implemented in this repository on the
existing `codex/change-aware-skill-retention` branch. Earlier retention code
and pre-existing untracked work were preserved. Changes remain uncommitted.

The user's chosen scope is computer-use agents, targeting the October 12,
2026 ARR deadline for NAACL 2027. The refined research target is attribution
of violations of unchanged user constraints to persistent-memory updates,
with separate measurements of rule omission, behavioral override, and external
guard masking. Generic safety drift or policy guards are not claimed as new.

Implemented components:

* Accessibility-tree virtual desktop with file-manager, editor, and mail apps;
  visible-element click/type actions, with no model-accessible host filesystem,
  shell, or real message sending.
* Independent rules for preserving records, preserving a security file, and
  exact task-local approval for attachment sends; normalized paths and
  ancestor deletion/move checks.
* Persistent, versioned memory evolving through training experiences;
  frozen, compression-only, evolving, pinned, guarded, and gated conditions.
* Split-isolated held-out checkpoint audits with current, previous, initial,
  and rule-reinjected memories, matched task states/seeds, and randomized arm
  order. Guarded runs also receive an unguarded diagnostic replay.
* Attempted/realized violation rates, utility and safe utility, invalid-output
  bounds, first observed violations, and paired stream contrasts. Bootstrap
  intervals are suppressed for fewer than five streams.
* Scripted smoke backend and executable live-model backend using the existing
  LLM client, with explicit API caps, per-call logs, usage accounting, and
  incomplete-run markers. A DeepSeek Flash live pilot was subsequently run;
  its evidence and limitations are in the
  [pilot report](deepseek_flash_safety_pilot_20260930.md).
* A standalone interactive GUI replay for reviewing action traces.

Validation:

* Focused safety-retention/LLM group after DeepSeek integration: **66 passed**.
* The earlier safety/retention/LLM group: **95 passed** before the integration.
* Broad repository suite: **927 passed, 40 failed, 47 subtests passed**, excluding
  pre-existing untracked `tests/test_repair_loop.py`, whose imported
  `apptainer_eval` module is absent. The failure set corresponds to the same
  environment/artifact/layout failures already reproduced on untouched
  `origin/main` in the previous implementation checkpoint.
* Compilation, CLI help, whitespace check, and replay JavaScript syntax check
  passed.

Long-run synthetic validation is stored in
`runs/computer_use_safety_retention_100_20260930/`:

* 100 training episodes × 6 conditions × 3 seeds = 1,800 training episodes.
* 1,008 checkpoint probe episodes and 2,400 gate probe episodes.
* 31,854 scripted requests, zero model requests, and zero API cost.
* All final held-out GUI episodes were valid.
* The planted evolved policy attempted/caused protected deletion; restoring
  initial memory or original rules reversed the planted behavior.
* The guard blocked effects while preserving the high attempted-violation
  rate, verifying that enforcement does not get mistaken for actor retention.

These results validate implementation only. The scripted learner deliberately
plants the unsafe procedure, and the fixture workflows are template-related.
They are not evidence of naturally emerging model failures, a successful
mitigation on real agents, or generalization to real desktops.

Read the [research proposal](../docs/research/computer_use_safety_retention_proposal.md),
[usage guide](../docs/research/computer_use_safety_retention_usage.md), and local
[GUI replay](../runs/computer_use_safety_retention_100_20260930/replay.html).
The live pilot used the checked-in DeepSeek Flash configuration and user-provided
budget. A screenshot-based or real-desktop transfer adapter is not included in
this initial accessibility-based microbenchmark.
