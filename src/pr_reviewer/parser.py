from __future__ import annotations

import re
from pathlib import PurePosixPath

from .models import ChangedLine, DiffFile, DiffHunk

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _safe_path(path: str) -> str:
    path = path.replace("\\", "/")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or not path or "\x00" in path:
        raise ValueError(f"Unsafe diff path: {path!r}")
    return candidate.as_posix()


def parse_diff(raw: str, *, max_bytes: int = 5_000_000) -> list[DiffFile]:
    """Parse a Git unified diff and retain GitHub's 1-based patch positions."""
    if len(raw.encode("utf-8")) > max_bytes:
        raise ValueError("Diff exceeds configured size limit")
    files: list[DiffFile] = []
    current: DiffFile | None = None
    hunk: DiffHunk | None = None
    old_line = new_line = None
    position = 0
    patch_rows: list[str] = []

    def finish_file() -> None:
        nonlocal patch_rows
        if current is not None:
            current.patch = "\n".join(patch_rows)
        patch_rows = []

    for row in raw.splitlines():
        header = DIFF_HEADER.match(row)
        if header:
            finish_file()
            old_path, new_path = map(_safe_path, header.groups())
            current = DiffFile(path=new_path, old_path=old_path)
            files.append(current)
            hunk = None
            old_line = new_line = None
            position = 0
            continue
        if current is None:
            continue
        if row.startswith("rename from "):
            current.old_path = _safe_path(row.removeprefix("rename from "))
            current.status = "renamed"
            continue
        if row.startswith("rename to "):
            current.path = _safe_path(row.removeprefix("rename to "))
            current.status = "renamed"
            continue
        if row.startswith("new file mode"):
            current.status = "added"
            continue
        if row.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if row.startswith("+++ "):
            target = row[4:]
            if target == "/dev/null":
                current.status = "deleted"
            elif target.startswith("b/"):
                current.path = _safe_path(target[2:])
            else:
                raise ValueError("Malformed new-file header")
            continue
        match = HUNK.match(row)
        if row.startswith("@@"):
            if not match:
                raise ValueError("Malformed hunk header")
            old_start, old_count, new_start, new_count = match.groups()
            hunk = DiffHunk(
                header=row,
                old_start=int(old_start),
                old_count=int(old_count or 1),
                new_start=int(new_start),
                new_count=int(new_count or 1),
            )
            current.hunks.append(hunk)
            old_line, new_line = int(old_start), int(new_start)
            patch_rows.append(row)
            position += 1
            continue
        if hunk is None or old_line is None or new_line is None:
            continue
        if row == "\\ No newline at end of file":
            patch_rows.append(row)
            continue
        position += 1
        patch_rows.append(row)
        changed: ChangedLine | None = None
        if row.startswith("+"):
            changed = ChangedLine(new_line=new_line, position=position, kind="ADD", text=row[1:])
            new_line += 1
        elif row.startswith("-"):
            changed = ChangedLine(old_line=old_line, position=position, kind="DELETE", text=row[1:])
            old_line += 1
        elif row.startswith(" "):
            changed = ChangedLine(
                old_line=old_line,
                new_line=new_line,
                position=position,
                kind="CONTEXT",
                text=row[1:],
            )
            old_line += 1
            new_line += 1
        if changed is not None:
            hunk.lines.append(changed)
            current.lines.append(changed)
    finish_file()
    return files
