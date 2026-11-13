from __future__ import annotations

import ast
import textwrap

from .models import DiffFile, Finding, Suggestion


class RefactoringEngine:
    def validate(self, finding: Finding, diff_file: DiffFile) -> Suggestion | None:
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

    def validate_all(self, findings: list[Finding], files: list[DiffFile]) -> None:
        by_path = {item.path: item for item in files}
        for finding in findings:
            if finding.path in by_path and finding.suggestion:
                if self.validate(finding, by_path[finding.path]) is None:
                    finding.suggestion = None
