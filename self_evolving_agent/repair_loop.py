"""Execution-feedback repair loop (Trace2Repair, arm A1).

The attribution study showed that on a strong single-shot Agentless solver most
failures happen *after* correct localization: the agent had the right file and
wrote the wrong patch, and no form of task-start context (memory, hints,
full-source injection) converts those. What crosses that barrier is execution
feedback: run a verification signal, show the model the actual error, and let it
repair its own patch.

Leak hygiene: the repair signal never touches the hidden FAIL_TO_PASS /
PASS_TO_PASS tests or the oracle test patch. Instead the model writes a
*reproduction script* from the issue text alone; the script is admitted as an
oracle only if it FAILS on the unpatched code (the self-authored-oracle
admission rule). Repro runs execute in the same Apptainer container as official
scoring but apply only the model patch, never the test patch.

All LLM and container calls are injectable, so orchestration is unit-testable
with fakes; the container runner reuses the validated overlay+bind recipe from
:mod:`apptainer_eval`.
"""

from __future__ import annotations

import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from self_evolving_agent.apptainer_eval import (
    DEFAULT_OVERLAY_SIZE_MB,
    DEFAULT_SIF_DIR,
    InstanceEvalSpec,
    sif_path_for,
    _default_runner,
    _safe_rmtree,
)

CONDA_ACTIVATE = "source /opt/miniconda3/bin/activate testbed"
TESTBED = "/testbed"

DEFAULT_REPRO_TIMEOUT_S = 600
DEFAULT_MAX_REPAIR_ROUNDS = 2
_EXIT_MARK = re.compile(r"@@REPRO_EXIT=(-?\d+)@@")

# A base failure matching these is an environment/import problem, not the bug:
# the repro couldn't even exercise the reported behavior, so it is not a valid
# oracle (seen e.g. as a pandas OptionError from a version-mismatched repro).
_ENV_ERROR_RE = re.compile(
    r"\b(ModuleNotFoundError|ImportError|OptionError|DistributionNotFound)\b"
)


def _looks_env_error(output: str) -> bool:
    return bool(_ENV_ERROR_RE.search(output or ""))


@dataclass
class ReproResult:
    """Outcome of one repro-script run inside the container."""

    ran: bool
    exit_code: int | None
    output: str
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.ran and self.exit_code == 0


@dataclass
class RepairOutcome:
    """Audit record of the full repair attempt for one task."""

    task_id: str
    final_patch: str
    rounds_used: int
    repro_admitted: bool = False
    repro_source: str = ""
    initial_repro_passed: bool | None = None
    round_results: list[dict[str, Any]] = field(default_factory=list)
    error_signatures: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Repro synthesis (LLM; issue text only — never sees oracle tests)
# --------------------------------------------------------------------------

def build_repro_messages(task: Any) -> list[dict[str, str]]:
    issue = str(getattr(task, "prompt", "") or getattr(task, "issue_description", ""))
    repo = str((getattr(task, "metadata", None) or {}).get("repo", ""))
    system = (
        "You write minimal reproduction scripts for reported software bugs. "
        "Given a GitHub issue, produce ONE standalone Python script that "
        "reproduces the reported bug when run from the repository root with the "
        "package importable. Contract: the script must (a) print 'REPRO PASS' "
        "and exit 0 if the described behavior is CORRECT/FIXED, and (b) FAIL "
        "with an uncaught AssertionError if the bug is PRESENT - assert the "
        "correct expected behavior so a failure raises a real traceback (do NOT "
        "just print and sys.exit; the assertion's traceback is the signal). "
        "Target the SPECIFIC behavior the issue describes, not a broad smoke "
        "check, so the script distinguishes a real fix from an unrelated patch. "
        "Use only the issue text; do not import or reference the project's test "
        "suite, pytest fixtures, or files under tests/, and use only APIs the "
        "issue itself mentions (avoid version-specific calls). Keep it under 80 "
        "lines, standard library plus the project package only. Return ONLY the "
        "Python code, no markdown fences, no prose."
    )
    user = f"Repository: {repo}\n\nIssue:\n{issue}\n\nWrite the reproduction script."
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def extract_script(text: str) -> str:
    """Pull Python source from a model response (tolerates ``` fences)."""

    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    fence = re.search(r"```(?:python)?\s*\n(.*?)```", stripped, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return stripped


# --------------------------------------------------------------------------
# Container repro runner (mirrors score_prediction's validated recipe)
# --------------------------------------------------------------------------

def build_repro_script(spec: InstanceEvalSpec, *, has_model_patch: bool) -> str:
    """Bash run inside the container: reset, apply model patch only, run repro.

    The oracle test patch is NEVER applied here (leak hygiene); the repro is the
    model's own script, bound in via /host.
    """

    if has_model_patch:
        model_apply = (
            "if git apply /host/model.patch 2>/host/model.err || "
            "git apply --3way /host/model.patch 2>>/host/model.err; then "
            "echo '@@MODEL_APPLIED@@'; else echo '@@MODEL_APPLY_FAILED@@'; fi\n"
        )
    else:
        model_apply = "echo '@@NO_MODEL_PATCH@@'\n"
    return (
        f"{CONDA_ACTIVATE}\n"
        f"cd {TESTBED}\n"
        f"git reset --hard {shlex.quote(spec.base_commit)} -q\n"
        "git clean -fdq\n"
        f"{model_apply}"
        "echo '@@REPRO_START@@'\n"
        "python /host/repro.py 2>&1\n"
        "echo \"@@REPRO_EXIT=$?@@\"\n"
    )


def run_repro(
    spec: InstanceEvalSpec,
    model_patch: str | None,
    repro_source: str,
    *,
    sif_dir: str | Path = DEFAULT_SIF_DIR,
    overlay_size_mb: int = DEFAULT_OVERLAY_SIZE_MB,
    timeout_s: int = DEFAULT_REPRO_TIMEOUT_S,
    work_dir: str | Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> ReproResult:
    """Run the repro script in the instance container with the patch applied."""

    runner = runner or _default_runner
    sif = sif_path_for(spec, sif_dir)
    if not sif.is_file():
        return ReproResult(ran=False, exit_code=None, output="", error=f"SIF not found: {sif}")

    has_model_patch = bool(model_patch and model_patch.strip())
    tmp_root = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="repro_"))
    tmp_root.mkdir(parents=True, exist_ok=True)
    host_dir = tmp_root / spec.instance_id
    host_dir.mkdir(parents=True, exist_ok=True)
    (host_dir / "repro.py").write_text(repro_source, encoding="utf-8")
    (host_dir / "run.sh").write_text(
        build_repro_script(spec, has_model_patch=has_model_patch), encoding="utf-8"
    )
    if has_model_patch:
        (host_dir / "model.patch").write_text(model_patch, encoding="utf-8")
    overlay = host_dir / "overlay.img"
    if overlay.exists():
        overlay.unlink()
    try:
        create = runner(
            ["apptainer", "overlay", "create", "--size", str(overlay_size_mb), str(overlay)],
            timeout=None,
        )
        if getattr(create, "returncode", 0) != 0:
            return ReproResult(
                ran=False, exit_code=None, output="",
                error=f"overlay create failed: {getattr(create, 'stdout', '')[-500:]}",
            )
        cmd = [
            "apptainer", "exec",
            "--overlay", str(overlay),
            "--cleanenv",
            "--bind", f"{host_dir}:/host",
            str(sif),
            "bash", "-l", "/host/run.sh",
        ]
        result = runner(cmd, timeout=timeout_s)
        stdout = getattr(result, "stdout", "") or ""
        m = _EXIT_MARK.search(stdout)
        if not m:
            return ReproResult(ran=False, exit_code=None, output=stdout[-4000:],
                               error="no repro exit marker")
        start = stdout.find("@@REPRO_START@@")
        body = stdout[start + len("@@REPRO_START@@"):m.start()] if start != -1 else stdout
        return ReproResult(ran=True, exit_code=int(m.group(1)), output=body.strip()[-4000:])
    finally:
        if work_dir is None:
            _safe_rmtree(tmp_root)


def admit_repro(
    spec: InstanceEvalSpec,
    repro_source: str,
    **run_kwargs: Any,
) -> tuple[bool, ReproResult]:
    """Base-side admission: the repro must FAIL on unpatched code with a *bug*
    error (not an environment/import error).

    This is necessary but not sufficient: :func:`repair_task` additionally
    requires the repro to fail on the *current known-failing patch* so that a
    non-discriminating repro (one that passes on a patch we know is wrong) is
    rejected.
    """

    base = run_repro(spec, None, repro_source, **run_kwargs)
    admitted = base.ran and base.exit_code not in (0, None) and not _looks_env_error(base.output)
    return admitted, base


# --------------------------------------------------------------------------
# Error signatures + repair prompt
# --------------------------------------------------------------------------

def extract_error_signature(output: str) -> str:
    """Normalized signature of a repro failure (exception type + message head).

    Used to key the repair-memory bank: the same signature on a future task
    retrieves the repair lessons learned here.
    """

    if not output:
        return "no_output"
    lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
    # Last raised exception line, e.g. "AssertionError: ..." / "TypeError: ...".
    for ln in reversed(lines):
        m = re.match(r"([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning))\b:?\s*(.*)", ln)
        if m:
            head = re.sub(r"[0-9]+", "N", m.group(2))[:80]
            return f"{m.group(1)}::{head}".rstrip(":")
    return re.sub(r"[0-9]+", "N", lines[-1])[:100]


def build_repair_messages(
    task: Any,
    current_patch: str,
    repro_output: str,
    *,
    context: Sequence[Mapping[str, Any]] | None = None,
    repro_source: str = "",
    repair_lessons: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """Repair prompt: issue + failing repro output + current patch -> new diff.

    ``repair_lessons`` is the error-conditioned memory hook (arm A3): lessons
    retrieved by the observed error signature are injected HERE, at repair time,
    not at task start.
    """

    issue = str(getattr(task, "prompt", "") or "")
    system = (
        "You repair software patches using execution feedback. You proposed a "
        "patch for the issue below; an independent reproduction script still "
        "fails (or the patch was empty/inapplicable). Analyze the reproduction "
        "output and return a corrected, COMPLETE git unified diff against the "
        "ORIGINAL (unpatched) repository state - not a diff on top of the "
        "previous patch. Output requirements: start with 'diff --git'; no "
        "markdown fences, no prose; include every needed hunk; never modify "
        "tests, configuration, or the reproduction script."
    )
    user_lines = [
        f"Issue:\n{issue}",
        "",
        "Your previous patch (may be empty):",
        current_patch.strip() or "(empty patch)",
        "",
        "Reproduction script output after applying your patch:",
        repro_output.strip() or "(no output)",
    ]
    if repro_source:
        user_lines += ["", "Reproduction script source:", repro_source.strip()]
    if repair_lessons:
        user_lines += [
            "",
            "Repair strategies that fixed this error pattern before "
            "(apply if relevant):",
        ]
        user_lines += [f"- {lesson}" for lesson in repair_lessons]
    if context:
        user_lines.append("")
        user_lines.append("Relevant repository source:")
        for item in context:
            user_lines.append(
                f"<file path=\"{item.get('path', '')}\">\n{item.get('content', '')}\n</file>"
            )
    user_lines += ["", "Return only the corrected unified diff."]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def repair_task(
    task: Any,
    spec: InstanceEvalSpec,
    initial_patch: str,
    *,
    llm_chat: Callable[..., Any],
    max_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS,
    max_completion_tokens: int = 8192,
    context: Sequence[Mapping[str, Any]] | None = None,
    retrieve_lessons: Callable[[str], list[str]] | None = None,
    run_repro_fn: Callable[..., ReproResult] | None = None,
    admit_fn: Callable[..., tuple[bool, ReproResult]] | None = None,
    extract_diff_fn: Callable[[str], str] | None = None,
    repro_source: str | None = None,
    synth_repro_fn: Callable[[Any], str] | None = None,
    **run_kwargs: Any,
) -> RepairOutcome:
    """Full per-task repair: synth+admit repro, then iterate patch on failure.

    Returns the final patch (unchanged if the repro is inadmissible or already
    passes). ``retrieve_lessons(signature)`` is the A3 memory hook; pass None
    for the memory-free A1 arm.
    """

    task_id = str(getattr(task, "task_id", ""))
    run_repro_impl = run_repro_fn or run_repro
    admit_impl = admit_fn or admit_repro
    if extract_diff_fn is None:
        from self_evolving_agent.agent import _extract_unified_diff as extract_diff_fn

    # 1. Reproduction oracle (issue text only).
    source = repro_source
    if source is None:
        if synth_repro_fn is not None:
            source = synth_repro_fn(task)
        else:
            resp = llm_chat(build_repro_messages(task),
                            max_completion_tokens=max_completion_tokens)
            source = extract_script(getattr(resp, "content", "") or "")
    if not (source or "").strip():
        return RepairOutcome(task_id, initial_patch, 0, repro_admitted=False)

    # 2a. Base-side admission: must fail on unpatched code with a bug (not env) error.
    admitted_base, _base = admit_impl(spec, source, **run_kwargs)
    if not admitted_base:
        return RepairOutcome(task_id, initial_patch, 0, repro_admitted=False,
                             repro_source=source)

    outcome = RepairOutcome(task_id, initial_patch, 0, repro_source=source)
    patch = initial_patch

    # 2b. Discrimination check: the current patch is KNOWN to not resolve the
    # task (it failed the official tests). A trustworthy repro must therefore
    # also fail on it. If it passes, the repro is not capturing the real bug
    # (false-admit) -> reject and leave the patch unchanged.
    check = run_repro_impl(spec, patch, source, **run_kwargs)
    outcome.initial_repro_passed = check.passed
    if check.passed:
        outcome.repro_admitted = False
        return outcome  # non-discriminating repro; do not trust it
    outcome.repro_admitted = True

    # 3. Repair rounds.
    for round_idx in range(max_rounds):
        signature = extract_error_signature(check.output)
        outcome.error_signatures.append(signature)
        lessons = retrieve_lessons(signature) if retrieve_lessons else None
        messages = build_repair_messages(
            task, patch, check.output,
            context=context, repro_source=source, repair_lessons=lessons,
        )
        try:
            resp = llm_chat(messages, max_completion_tokens=max_completion_tokens)
        except Exception as exc:  # pragma: no cover - provider errors vary
            outcome.round_results.append({"round": round_idx, "error": str(exc)[:200]})
            break
        new_patch = extract_diff_fn(getattr(resp, "content", "") or "")
        if not new_patch.strip():
            outcome.round_results.append({"round": round_idx, "empty_repair": True})
            break
        patch = new_patch
        check = run_repro_impl(spec, patch, source, **run_kwargs)
        outcome.rounds_used = round_idx + 1
        outcome.round_results.append({
            "round": round_idx,
            "signature": signature,
            "repro_passed": check.passed,
            "repro_exit": check.exit_code,
        })
        outcome.final_patch = patch
        if check.passed:
            break
    return outcome
