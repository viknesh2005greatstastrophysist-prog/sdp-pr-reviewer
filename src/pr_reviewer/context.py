from __future__ import annotations

import ast
from pathlib import Path

from .models import CodeContext, DiffFile


def _within(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


class PythonContextExtractor:
    """Extract syntax context without importing or executing repository code."""

    def __init__(self, max_file_bytes: int = 1_000_000, max_context_lines: int = 120):
        self.max_file_bytes = max_file_bytes
        self.max_context_lines = max_context_lines

    def enrich(self, files: list[DiffFile], repo: str | Path) -> list[str]:
        root = Path(repo).resolve()
        warnings: list[str] = []
        for diff_file in files:
            if not diff_file.path.endswith(".py") or diff_file.status == "deleted":
                continue
            target = (root / diff_file.path).resolve()
            if not _within(root, target) or not target.is_file():
                warnings.append(f"Context unavailable for {diff_file.path}")
                continue
            if target.stat().st_size > self.max_file_bytes:
                warnings.append(f"Context skipped for oversized file {diff_file.path}")
                continue
            try:
                source = target.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=diff_file.path)
            except (OSError, UnicodeError, SyntaxError) as exc:
                warnings.append(f"Context parse failed for {diff_file.path}: {exc}")
                continue
            source_lines = source.splitlines()
            nodes = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and hasattr(node, "end_lineno")
            ]
            for line in diff_file.added_lines():
                matches = [n for n in nodes if n.lineno <= line <= (n.end_lineno or n.lineno)]
                if not matches:
                    continue
                node = min(matches, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)
                end = min(node.end_lineno or node.lineno, node.lineno + self.max_context_lines - 1)
                diff_file.contexts[line] = CodeContext(
                    symbol=getattr(node, "name", None),
                    node_type=type(node).__name__,
                    start_line=node.lineno,
                    end_line=end,
                    source="\n".join(source_lines[node.lineno - 1 : end]),
                )
        return warnings
