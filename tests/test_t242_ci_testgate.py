"""T-242: the build-wheel CI runs pytest + ruff BEFORE build/publish.

Audit F7: .github/workflows/build-wheel.yml had ZERO pytest/ruff
references — 38 test files shipped a wheel without any gate, and a red
suite still built a release. The workflow now has a ``test`` job
(pytest -q + ruff check, python 3.11 + 3.12 matrix) that
``build-wheel`` needs, which in turn gates ``publish-pypi``.

Ruff contract: an explicit, deterministic select (``E9,F821,F811``) —
green on today's tree by design; the wider lint debt (84 findings under
ruff 0.16's expanded default select, 62 auto-fixable) is a separate
task and must NOT turn the pipeline red. W292 etc. are equally
pre-existing and stay out of the gate.

Also verifies: the invalid ``# noqa: unreachable`` directives
(tests/test_backfill_pending_tasks.py:44,
tests/test_t186_sse_timeouts.py:62 — ruff rejects that directive form)
are gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build-wheel.yml"
)


def _workflow_jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_workflow_has_test_job_gating_the_build():
    jobs = _workflow_jobs()
    assert "test" in jobs, "the wheel must not build before the suite runs"
    test_job = jobs["test"]
    steps = [step.get("run", "") for step in test_job["steps"] if isinstance(step, dict)]
    joined = "\n".join(steps)
    # The suite needs its deps: the package + pytest + ruff must be
    # installed BEFORE the pytest step runs (a lost install step = the
    # gate fails 'No module named pytest' — T-242 live-run finding).
    assert any("pip install" in s and "." in s.split("pip install")[-1] for s in steps), (
        "the test job must install the package before pytest"
    )
    assert "pytest" in joined
    assert "ruff check" in joined
    # Audit ask: py 3.11 + 3.12.
    matrix = test_job["strategy"]["matrix"]["python-version"]
    assert {str(v) for v in matrix} >= {"3.11", "3.12"}


def test_build_and_publish_need_the_test_job():
    jobs = _workflow_jobs()
    needs = jobs["build-wheel"].get("needs")
    assert "test" in (needs if isinstance(needs, list) else [needs])
    pypi_needs = jobs["publish-pypi"].get("needs")
    assert pypi_needs in ("build-wheel", ["build-wheel"])


def test_ruff_select_is_explicit_and_currently_green():
    """The CI's exact ruff select must pass on THIS tree — locks the gate
    baseline and guards against new high-signal findings (undefined
    names, syntax errors, redefinitions)."""
    import shutil
    import subprocess
    import sys

    # Prefer the interpreter's own ruff (venv), fall back to PATH.
    ruff = Path(sys.executable).parent / "ruff"
    if not ruff.exists():
        which = shutil.which("ruff")
        if which is None:
            pytest.skip("ruff not available on this interpreter")
        ruff = which
    proc = subprocess.run(
        [str(ruff), "check", "--select", "E9,F821,F811", "."],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_no_invalid_noqa_directives_remain():
    """ruff rejects ``# noqa: unreachable`` (not a comma-separated code
    list) — both same-class occurrences are fixed."""
    for rel in (
        "tests/test_backfill_pending_tasks.py",
        "tests/test_t186_sse_timeouts.py",
    ):
        text = (Path(__file__).resolve().parent.parent / rel).read_text(
            encoding="utf-8"
        )
        assert "# noqa: unreachable" not in text, rel