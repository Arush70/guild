"""Verification: codeguild runs the project's tests itself and reports facts. No model involved."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PYTEST_RX = re.compile(r"(\d+) passed|(\d+) failed|(\d+) error", re.I)
_NOT_RUN_RX = re.compile(r"(\d+) (?:skipped|xfailed|xpassed|deselected)")
_FAIL_LINE_RX = re.compile(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$", re.M)


@dataclass
class Baseline:
    """Which tests existed before the engineer touched anything. Tests that vanish afterwards
    mean existing behaviour was deleted, whatever the new tests say."""

    tests: set[str] | None  # pytest node ids; None when the runner is not pytest
    count: int | None  # number of tests run, when ids are not available


@dataclass
class Verification:
    passed: bool
    tests_run: int
    failures: list[str]
    output_tail: str
    command: str
    exit_code: int
    lint_passed: bool | None = None  # None: no lint command, or the tests already failed
    removed_tests: list[str] = field(default_factory=list)


class Verifier:
    def __init__(self, sandbox: Any, test_command: str, lint_command: str | None = None):
        self.sandbox = sandbox
        self.test_command = test_command
        self.lint_command = lint_command

    def collect_tests(self) -> set[str] | None:
        """Set of pytest node ids (file::test). None when the test runner isn't pytest."""
        cmd = self.test_command
        if "pytest" not in cmd:
            return None
        # exactly one -q: with two, pytest prints per-file counts instead of node ids
        base = " ".join(t for t in cmd.split() if t not in ("-q", "--quiet", "-qq"))
        res = self.sandbox.run(f"{base} --collect-only -q", timeout=300)
        return {
            ln.strip()
            for ln in res.output.splitlines()
            if "::" in ln and not ln.startswith(("=", "ERROR", "E "))
        }

    def baseline(self) -> Baseline:
        tests = self.collect_tests()
        return Baseline(tests, self.run().tests_run if tests is None else None)

    def run(self, baseline: Baseline | None = None) -> Verification:
        """Run the test command (then the lint command). With a baseline, also fail when tests
        that existed before are gone."""
        cmd = self.test_command
        res = self.sandbox.run(cmd, timeout=600)
        out = res.output
        passed_n = failed_n = 0
        for m in _PYTEST_RX.finditer(out):
            passed_n += int(m.group(1) or 0)
            failed_n += int(m.group(2) or 0) + int(m.group(3) or 0)
        failures = [
            f"{m.group(2)}: {m.group(3) or ''}".strip(": ") for m in _FAIL_LINE_RX.finditer(out)
        ][:20]
        exit_code = res.returncode
        tail = "\n".join(out.strip().splitlines()[-25:])
        if exit_code == 5 and "pytest" in cmd:  # pytest's "no tests collected" is not a failure
            exit_code = 0
            tail += "\n(no tests collected)"
        if exit_code != 0 and not failures:
            failures = [f"{cmd} exited {exit_code}"]
        # a run that reports failed tests has failed, whatever exit code it ends with (a
        # conftest hook can set the exit code to 0)
        ok = exit_code == 0 and failed_n == 0
        if exit_code == 0 and failed_n and not failures:
            failures = [f"{failed_n} test(s) failed although {cmd} exited 0"]
        v = Verification(ok, passed_n + failed_n, failures, tail, cmd, exit_code)
        if self.lint_command and v.passed:
            lint = self.sandbox.run(self.lint_command, timeout=300)
            v.lint_passed = lint.returncode == 0
            if not v.lint_passed:
                v.passed = False
                v.failures.append(f"lint: {self.lint_command} exited {lint.returncode}")
                v.output_tail += "\n--- lint ---\n" + "\n".join(
                    lint.output.strip().splitlines()[-15:]
                )
        idle = sum(int(n) for n in _NOT_RUN_RX.findall(out))
        if v.passed and baseline and baseline.tests and passed_n + failed_n == 0 and idle:
            # every test skipped, xfailed or deselected: the suite "passes" without checking
            # anything (one conftest hook is enough to do that)
            v.passed = False
            v.failures.append(
                f"NO TEST RAN ({idle} skipped, xfailed or deselected): the tests that existed "
                "before the task must still run"
            )
        v.removed_tests = self._removed_tests(baseline, v) if baseline else []
        if v.removed_tests:
            v.passed = False
            v.failures.append(
                "EXISTING TESTS REMOVED (you must keep all pre-existing code and tests): "
                + ", ".join(v.removed_tests[:8])
            )
        return v

    def _removed_tests(self, baseline: Baseline, v: Verification) -> list[str]:
        if baseline.tests:
            return sorted(baseline.tests - (self.collect_tests() or set()))
        if baseline.count is not None and v.tests_run < baseline.count:
            return [f"test count dropped from {baseline.count} to {v.tests_run}"]
        return []
