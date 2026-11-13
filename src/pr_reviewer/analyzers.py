from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .models import DiffFile, Finding, Severity

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _relative_path(raw: str, root: Path) -> str:
    path = Path(raw)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root)
        except ValueError:
            return ""
    normalized = path.as_posix().removeprefix("./")
    return normalized if ".." not in Path(normalized).parts else ""


class StaticAnalyzer:
    """Run analyzer CLIs only; repository Python code is never executed or imported."""

    def __init__(self, timeout: float = 60, runner: Runner = subprocess.run):
        self.timeout = timeout
        self.runner = runner

    def analyze(
        self, files: list[DiffFile], repo: str | Path | None
    ) -> tuple[list[Finding], list[str]]:
        if repo is None:
            return [], ["Static analyzers require a trusted, isolated checkout."]
        root = Path(repo).resolve()
        if not root.is_dir():
            return [], ["Static analysis checkout does not exist."]
        changed = {item.path: item.added_lines() for item in files}
        findings: list[Finding] = []
        warnings: list[str] = []
        commands = {
            "ruff": [
                "ruff",
                "check",
                "--isolated",  # ignore ruff.toml / pyproject.toml shipped by the PR
                "--no-cache",
                "--select",
                "E,F,I,B,UP,S",
                "--output-format",
                "json",
                ".",
            ],
            "semgrep": [
                "semgrep",
                "scan",
                "--json",
                "--metrics=off",
                "--disable-version-check",
                "--x-ignore-semgrepignore-files",  # a PR must not exclude its own files
                "--no-git-ignore",
                "--config",
                str(Path(__file__).with_name("semgrep_rules.yml")),
                ".",
            ],
        }
        for tool, command in commands.items():
            if self.runner is subprocess.run:
                executable = shutil.which(tool)
                if executable is None:
                    sibling = Path(sys.executable).parent / (
                        f"{tool}.exe" if sys.platform == "win32" else tool
                    )
                    executable = str(sibling) if sibling.is_file() else None
                if executable is None:
                    warnings.append(f"{tool} not installed; skipped")
                    continue
                command[0] = executable
            try:
                process = self.runner(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                    shell=False,
                )
                payload = json.loads(process.stdout or ("[]" if tool == "ruff" else "{}"))
                entries = payload if tool == "ruff" else payload.get("results", [])
                for item in entries:
                    raw_path = item.get("filename", "") if tool == "ruff" else item.get("path", "")
                    path = _relative_path(raw_path, root)
                    line = (
                        item.get("location", {}).get("row", 0)
                        if tool == "ruff"
                        else item.get("start", {}).get("line", 0)
                    )
                    if not path or line not in changed.get(path, set()):
                        continue
                    rule = item.get("code") if tool == "ruff" else item.get("check_id")
                    if tool == "ruff" and is_test_path(path) and rule in TEST_EXEMPT_RULES:
                        continue  # asserts, test passwords, magic values: normal in tests
                    code = item.get("code") if tool == "ruff" else item.get("check_id")
                    message = (
                        item.get("message", "")
                        if tool == "ruff"
                        else item.get("extra", {}).get("message", "")
                    )
                    findings.append(
                        Finding(
                            path=path,
                            line=line,
                            category=static_category(tool, code),
                            severity=self._severity(tool, item),
                            title=f"{tool}: {code or 'diagnostic'}",
                            message=message or "Static analysis diagnostic",
                            evidence=(
                                f"Deterministic {tool} rule {code or 'unknown'} matched "
                                f"line {line}."
                            ),
                            source=tool,
                            confidence=1.0,
                            rule_id=code,
                        )
                    )
                if process.returncode not in (0, 1):
                    warnings.append(
                        f"{tool} exited with {process.returncode}: {process.stderr[:300]}"
                    )
            except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError) as exc:
                warnings.append(f"{tool} failed: {exc}")
        return findings, warnings

    @staticmethod
    def _severity(tool: str, item: dict) -> Severity:
        if tool == "ruff":
            code = str(item.get("code", ""))
            if code in LOW_RISK_SECURITY_RULES:
                return Severity.LOW
            return Severity.HIGH if code.startswith("S") else Severity.LOW
        value = str(item.get("extra", {}).get("severity", "WARNING")).upper()
        return {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM}.get(value, Severity.LOW)


def analyze(files: list[DiffFile], repo: str | None = None) -> tuple[list[Finding], list[str]]:
    return StaticAnalyzer().analyze(files, repo)


# Ruff rule prefixes mapped onto the project's four review axes. Security (S), likely bugs
# (B, F, E9, PLE) are correctness problems; PERF is performance; the rest is style.
_BUG_PREFIXES = ("S", "B", "F", "E9", "PLE", "PLW", "ASYNC", "BLE", "TRY")
_PERFORMANCE_PREFIXES = ("PERF", "C4")


def static_category(tool: str, code: str | None) -> str:
    if tool != "ruff":
        return "bug-hunter"  # local Semgrep rules target security and correctness defects
    rule = (code or "").upper()
    if rule.startswith(_PERFORMANCE_PREFIXES):
        return "performance"
    if rule.startswith(_BUG_PREFIXES):
        return "bug-hunter"
    return "style"


# Rules that projects conventionally exempt in tests (ruff per-file-ignores for tests/**).
TEST_EXEMPT_RULES = {"S101", "S105", "S106", "S107", "S108", "S311", "PLR2004"}
# Bandit-derived rules that flag a pattern, not a vulnerability, outside tests.
LOW_RISK_SECURITY_RULES = {"S101", "S311", "S110", "S112"}


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    parts = path.split("/")
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or any(part in {"tests", "test", "testing"} for part in parts[:-1])
    )
