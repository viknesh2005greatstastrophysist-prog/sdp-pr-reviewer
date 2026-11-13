from __future__ import annotations

from dataclasses import dataclass

from .agents import OfflineAgent


@dataclass(frozen=True)
class DemoCase:
    case_id: str
    title: str
    subtitle: str
    filename: str
    icon: str
    accent: str
    coverage: tuple[str, ...]
    source: str
    failing_agent: str | None = None

    @property
    def diff(self) -> str:
        lines = self.source.rstrip("\n").splitlines()
        additions = "\n".join(f"+{line}" for line in lines)
        return (
            f"diff --git a/{self.filename} b/{self.filename}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{self.filename}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n"
            f"{additions}\n"
        )

    def metadata(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "filename": self.filename,
            "icon": self.icon,
            "accent": self.accent,
            "coverage": list(self.coverage),
            "line_count": len(self.source.rstrip("\n").splitlines()),
        }


class FailingDemoAgent(OfflineAgent):
    """A controlled failure used to demonstrate per-agent fault isolation."""

    async def review(self, files):
        raise RuntimeError("controlled demo failure")


DEMO_CASES = {
    case.case_id: case
    for case in (
        DemoCase(
            case_id="full-spectrum",
            title="Full spectrum",
            subtitle="Security, performance, style and architecture signals in one review.",
            filename="app.py",
            icon="✦",
            accent="violet",
            coverage=("All agents", "Ruff", "Semgrep", "Suggestions"),
            source="""from __future__ import annotations

import ast
import time

GLOBAL_CACHE: dict[int, dict[str, object]] = {}


def evaluate(expression: str):
    return eval(expression)


def find_duplicates(values: list[int]) -> list[int]:
    duplicates = []
    for value in values:
        if values.count(value) > 1 and value not in duplicates:
            duplicates.append(value)
    return duplicates


async def load_profile(user_id: int):
    time.sleep(0.1)
    profile = {"id": user_id, "role": "admin"}
    GLOBAL_CACHE[user_id] = profile
    return profile
""",
        ),
        DemoCase(
            case_id="security",
            title="Security breach",
            subtitle="Dangerous evaluation and shell invocation trigger deterministic rules.",
            filename="security.py",
            icon="⌾",
            accent="coral",
            coverage=("Bug Hunter", "Ruff S", "Semgrep", "Critical"),
            source="""from __future__ import annotations

import ast
import subprocess


def calculate(expression: str):
    return eval(expression)


def run_report(report_name: str):
    return subprocess.run(f"report {report_name}", shell=True, check=True)
""",
        ),
        DemoCase(
            case_id="performance",
            title="Performance regression",
            subtitle="Quadratic collection work and blocking calls inside async code.",
            filename="performance.py",
            icon="↯",
            accent="amber",
            coverage=("Performance agent", "Complexity", "Async I/O"),
            source="""from __future__ import annotations

import time


def repeated(values: list[int]) -> list[int]:
    duplicates = []
    for value in values:
        if values.count(value) > 1 and value not in duplicates:
            duplicates.append(value)
    return duplicates


async def fetch_dashboard(user_id: int) -> dict[str, int]:
    time.sleep(1)
    return {"user_id": user_id}
""",
        ),
        DemoCase(
            case_id="maintainability",
            title="Maintainability debt",
            subtitle="Mutable defaults, wildcard imports and broad exception handling.",
            filename="style.py",
            icon="Aa",
            accent="cyan",
            coverage=("Style agent", "Ruff B", "Syntax", "Fix suggestion"),
            source="""from math import *


def collect(items: list[str] = []):
    try:
        items.append(str(sqrt(16)))
    except:
        pass
    return items
""",
        ),
        DemoCase(
            case_id="architecture",
            title="Scaling boundary",
            subtitle="Process-local global state couples request handling and storage.",
            filename="profiles.py",
            icon="⌘",
            accent="violet",
            coverage=("Architecture agent", "State", "Scalability"),
            source="""from __future__ import annotations

GLOBAL_PROFILE_CACHE: dict[int, dict[str, object]] = {}


def save_and_authorize(user_id: int, payload: dict[str, object]):
    payload["role"] = "admin"
    GLOBAL_PROFILE_CACHE[user_id] = payload
    return GLOBAL_PROFILE_CACHE[user_id]
""",
        ),
        DemoCase(
            case_id="clean",
            title="Clean pull request",
            subtitle="A review with no actionable evidence demonstrates abstention.",
            filename="calculator.py",
            icon="✓",
            accent="green",
            coverage=("Zero findings", "Agent abstention", "Clean summary"),
            source="""from __future__ import annotations


def percentage(value: float, total: float) -> float:
    if total == 0:
        return 0.0
    return value / total * 100
""",
        ),
        DemoCase(
            case_id="resilience",
            title="Agent fault isolation",
            subtitle="One controlled specialist failure while the remaining pipeline completes.",
            filename="resilient.py",
            icon="⚡",
            accent="coral",
            coverage=("Fault tolerance", "Warnings", "Partial success"),
            failing_agent="architecture",
            source="""from __future__ import annotations


def normalize(value: str) -> str:
    return value.strip().casefold()
""",
        ),
    )
}


def demo_agents(case: DemoCase) -> list[OfflineAgent]:
    categories = ("bug-hunter", "performance", "style", "architecture")
    return [
        FailingDemoAgent(category) if category == case.failing_agent else OfflineAgent(category)
        for category in categories
    ]
