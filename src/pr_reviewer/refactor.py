from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from .models import DiffFile, Finding, Suggestion


class RefactoringEngine:
    def validate(
        self, finding: Finding, diff_file: DiffFile, source: str | None = None
    ) -> Suggestion | None:
        """Accept a suggestion only if it is safe to apply as a GitHub suggestion.

        ``source`` is the head version of the file. When given, the replacement is applied
        to it and the whole patched file must still parse; a snippet that parses on its own
        can still break the file (wrong indentation, an orphaned block header).
        """
        suggestion = finding.suggestion
        if suggestion is None or finding.path != diff_file.path:
            return None
        allowed = diff_file.added_lines()
        selected = set(range(suggestion.start_line, suggestion.end_line + 1))
        if not selected or not selected.issubset(allowed) or len(selected) > 25:
            return None
        if finding.line not in selected:
            # GitHub anchors the comment on the suggestion range; it must cover the finding.
            return None
        if "\x00" in suggestion.replacement or len(suggestion.replacement) > 12_000:
            return None
        suggestion = suggestion.model_copy(
            update={
                "replacement": _reindent(suggestion.replacement, diff_file, suggestion.start_line)
            }
        )
        if diff_file.path.endswith(".py"):
            try:
                ast.parse(textwrap.dedent(suggestion.replacement))
            except SyntaxError:
                wrapped = "def _suggested_change():\n" + textwrap.indent(
                    textwrap.dedent(suggestion.replacement), "    "
                )
                try:
                    ast.parse(wrapped)
                except SyntaxError:
                    return None
            if source is not None and not _patched_file_parses(source, suggestion):
                return None
        validated = suggestion.model_copy(update={"validated": True})
        finding.suggestion = validated
        return validated

    @staticmethod
    def publishable_suggestion(finding: Finding, diff_file: DiffFile) -> Suggestion | None:
        """The suggestion only if it is validated, all on added lines and covers the finding."""
        suggestion = finding.suggestion
        if suggestion is None or not suggestion.validated:
            return None
        selected = set(range(suggestion.start_line, suggestion.end_line + 1))
        if not selected or not selected.issubset(diff_file.added_lines()):
            return None
        return suggestion if finding.line in selected else None

    @staticmethod
    def github_body(finding: Finding, include_suggestion: bool = True) -> str:
        body = (
            f"**{finding.title}** ({finding.severity.value})\n\n"
            f"{finding.message}\n\nEvidence: {finding.evidence}"
        )
        if include_suggestion and finding.suggestion and finding.suggestion.validated:
            body += (
                f"\n\n```suggestion\n{finding.suggestion.replacement}\n```\n\n"
                f"{finding.suggestion.rationale}"
            )
        return body

    def validate_all(
        self, findings: list[Finding], files: list[DiffFile], root: str | Path | None = None
    ) -> None:
        """Validate every suggestion; ``root`` is the checkout holding the head files."""
        by_path = {item.path: item for item in files}
        sources: dict[str, str | None] = {}
        for finding in findings:
            if finding.path in by_path and finding.suggestion:
                if root is not None and finding.path not in sources:
                    sources[finding.path] = _read_head(Path(root), finding.path)
                source = sources.get(finding.path)
                if self.validate(finding, by_path[finding.path], source) is None:
                    finding.suggestion = None


def _read_head(root: Path, path: str) -> str | None:
    target = (root / path).resolve()
    try:
        target.relative_to(root.resolve())
        return target.read_text(encoding="utf-8")
    except (ValueError, OSError, UnicodeDecodeError):
        return None


def _patched_file_parses(source: str, suggestion: Suggestion) -> bool:
    """Apply the replacement to the head file; it must still parse if the head did."""
    lines = source.split("\n")
    if suggestion.end_line > len(lines):
        return False
    try:
        ast.parse(source)
    except SyntaxError:
        return True  # head already broken; the snippet checks above are all we can do
    new = (
        suggestion.replacement.rstrip("\n").split("\n")
        if suggestion.replacement.strip("\n")
        else []
    )
    patched = lines[: suggestion.start_line - 1] + new + lines[suggestion.end_line :]
    try:
        ast.parse("\n".join(patched))
    except SyntaxError:
        return False
    return True


def _reindent(replacement: str, diff_file: DiffFile, start_line: int) -> str:
    """Re-base a replacement on the indentation of the first line it replaces.

    Models often return the fix flush-left; GitHub applies a suggestion verbatim, so a
    flush-left fix inside a function would break the file. Relative indentation is kept.
    """
    original = next((item.text for item in diff_file.lines if item.new_line == start_line), "")
    indent = original[: len(original) - len(original.lstrip())]
    body = textwrap.dedent(replacement.strip("\n"))
    return "\n".join((indent + row) if row.strip() else row for row in body.split("\n"))
