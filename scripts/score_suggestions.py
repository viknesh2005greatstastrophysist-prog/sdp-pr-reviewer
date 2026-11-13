"""Offline quality scoring of validated suggestions in saved evaluation runs.

No LLM calls. For every unique validated suggestion it applies the replacement to the head
version of the file and measures what can be checked mechanically: that it applies, that the whole
patched file parses, whether a static rule it targets stops firing, whether it touches (and
restores) an injected defect, and whether it introduces new undefined-name or syntax errors.
None of this proves a fix is *correct*; see evaluation/RESULTS.md section 10.

Usage: python scripts/score_suggestions.py [--root .] [--out evaluation/scores/suggestions.json]
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import shutil
import subprocess
import tempfile
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

RESULT_FILES = [
    "results_injected_all60.json",
    "results_injected_paired.json",
    "results_human_test_gemini-3-flash-preview_postfix.json",
    "results_human_paired.json",
]
STATIC_TOOLS = {"ruff", "semgrep"}
RUFF_CODE = re.compile(r"^[A-Z]{1,3}\d{3,4}$")
METRICS = [
    "applies",
    "whole_file_parses",
    "resolves_static",
    "hits_defect",
    "restores_original",
    "changes_defect_line",
    "no_collateral",
]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def norm(text: str) -> str:
    return " ".join(text.split())


def norm_lines(lines: list[str]) -> list[str]:
    return [norm(x) for x in lines if x.strip()]


def split_lines(text: str) -> list[str]:
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


def apply_suggestion(
    source: str, start: int, end: int, replacement: str
) -> tuple[str | None, list[str]]:
    """Replace lines start..end (1-based, inclusive). Returns (patched or None, new lines)."""
    lines = split_lines(source)
    new = replacement.rstrip("\n").split("\n") if replacement.strip("\n") else []
    if not (1 <= start <= end <= len(lines)):
        return None, new
    patched = lines[: start - 1] + new + lines[end:]
    return "\n".join(patched) + ("\n" if source.endswith("\n") else ""), new


def classify_source(sources: set[str]) -> str:
    """'static' only if every contributing finding came purely from ruff/semgrep."""
    return "static" if any(set(s.split("+")) <= STATIC_TOOLS for s in sources) else "agent"


def load_unique(eval_dir: Path) -> list[dict[str, Any]]:
    uniq: dict[tuple, dict[str, Any]] = {}
    for name in RESULT_FILES:
        path = eval_dir / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        # Only `predictions`: `false_positive_adjudication` re-lists the same suggestions.
        for strategy, cases in data.get("predictions", {}).items():
            for case_id, res in cases.items():
                for f in res.get("findings", []):
                    s = f.get("suggestion")
                    if not s or not s.get("validated"):
                        continue
                    key = (case_id, f["path"], s["start_line"], s["end_line"], s["replacement"])
                    u = uniq.setdefault(
                        key,
                        {
                            "case_id": case_id,
                            "path": f["path"],
                            "start_line": s["start_line"],
                            "end_line": s["end_line"],
                            "replacement": s["replacement"],
                            "finding_line": f["line"],
                            "sources": set(),
                            "rule_ids": set(),
                            "seen_in": set(),
                        },
                    )
                    u["sources"].add(f.get("source", ""))
                    if f.get("rule_id"):
                        u["rule_ids"].add(f["rule_id"])
                    u["seen_in"].add(f"{name}:{strategy}")
    out = []
    for u in uniq.values():
        u["benchmark"] = "injected" if u["case_id"].startswith("inj__") else "human"
        u["source_class"] = classify_source(u["sources"])
        out.append(u)
    return sorted(out, key=lambda x: (x["case_id"], x["path"], x["start_line"], x["end_line"]))


def ruff_json(source: str, select: str) -> list[dict[str, Any]] | None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "f.py"
        p.write_text(source)
        r = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ruff",
                "check",
                "--isolated",
                "--select",
                select,
                "--output-format",
                "json",
                "--no-cache",
                str(p),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None


def parses(source: str) -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(source)
        return True
    except (SyntaxError, ValueError):
        return False


def error_counter(source: str) -> Counter | None:
    res = ruff_json(source, "E9,F")
    if res is None:
        return None
    return Counter((x["code"], x["message"]) for x in res)


def injection_info(eval_dir: Path, case_id: str) -> dict[str, Any] | None:
    for line in (eval_dir / "injection_corpus.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row["case_id"] == case_id:
            return row
    return None


def score_one(u: dict[str, Any], root: Path, inj_rows: dict[str, Any]) -> dict[str, Any]:
    base = root / ("evaluation/injected" if u["benchmark"] == "injected" else "evaluation/repos")
    fpath = base / u["case_id"] / u["path"]
    rec: dict[str, Any] = {
        k: (sorted(v) if isinstance(v, set) else v) for k, v in u.items() if k != "replacement"
    }
    rec["replacement"] = u["replacement"]
    rec.update({m: None for m in METRICS})
    if not fpath.exists():
        rec["note"] = "head file not found"
        return rec
    src = fpath.read_text()
    patched, new_lines = apply_suggestion(src, u["start_line"], u["end_line"], u["replacement"])
    rec["applies"] = patched is not None
    if patched is None:
        return rec
    rec["whole_file_parses"] = parses(patched)
    rec["head_parses"] = parses(src)  # context: failures on a file that already failed are not new

    # Static rule resolution (ruff codes only; semgrep rules are not re-run).
    code = next((r for r in sorted(u["rule_ids"]) if RUFF_CODE.match(r)), None)
    if code and fpath.suffix == ".py":
        rec["ruff_code"] = code
        before, after = ruff_json(src, code), ruff_json(patched, code)
        if before is not None and after is not None:
            delta = len(new_lines) - (u["end_line"] - u["start_line"] + 1)
            lo, hi = u["finding_line"] - 2, u["finding_line"] + 2
            fired = any(lo <= x["location"]["row"] <= hi for x in before)
            if fired:
                rec["resolves_static"] = not any(
                    lo <= x["location"]["row"] <= hi + max(delta, 0) for x in after
                )

    # Injected-defect checks.
    if u["benchmark"] == "injected":
        row = inj_rows.get(u["case_id"])
        if row:
            md = row["metadata"]
            line = md["injected_line"]
            same_file = u["path"] == md["source_file"]
            rec["hits_defect"] = bool(same_file and u["start_line"] <= line <= u["end_line"])
            src_lines = split_lines(src)
            head_ok = (
                same_file
                and line <= len(src_lines)
                and (norm(src_lines[line - 1]) == norm(md["line_after"]))
            )
            if rec["hits_defect"] and head_ok:
                orig = list(src_lines)
                orig[line - 1] = md["line_before"]
                want = norm_lines(orig[u["start_line"] - 1 : u["end_line"]])
                rec["restores_original"] = norm_lines(new_lines) == want
                rec["changes_defect_line"] = norm(md["line_after"]) not in {
                    norm(x) for x in new_lines
                }
            elif rec["hits_defect"]:
                rec["note"] = "head line does not match recorded line_after; original unrecoverable"
            else:
                rec["restores_original"] = False
                rec["changes_defect_line"] = False

    # Collateral: patched file must parse and gain no new E9/F errors.
    b, a = error_counter(src), error_counter(patched)
    if b is not None and a is not None:
        new_errors = a - b
        rec["new_ruff_E9_F"] = [f"{c}: {m}" for (c, m) in new_errors]
        rec["no_collateral"] = bool(rec["whole_file_parses"]) and not new_errors
    return rec


def rate(recs: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    vals = [r[metric] for r in recs if r.get(metric) is not None]
    k, n = sum(1 for v in vals if v), len(vals)
    ci = wilson(k, n)
    return {
        "k": k,
        "n": n,
        "rate": (k / n) if n else None,
        "wilson95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
    }


def summarise(recs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n_suggestions": len(recs)}
    for m in METRICS:
        r = rate(recs, m)
        if r["rate"] is not None:
            r["rate"] = round(r["rate"], 4)
        out[m] = r
    return out


def through_validator(
    u: dict[str, Any], root: Path, diffs: dict[str, str]
) -> dict[str, Any] | None:
    """Run a saved suggestion through the production validator (refactor.py).

    Returns the suggestion as the live service would publish it (re-indented), or None if
    the validator rejects it. Saved runs predate validation in the evaluation path.
    """
    import sys

    sys.path.insert(0, str(root / "src"))
    from pr_reviewer.models import Finding, Severity, Suggestion
    from pr_reviewer.parser import parse_diff
    from pr_reviewer.refactor import RefactoringEngine

    diff = diffs.get(u["case_id"])
    if diff is None:
        return None
    files = [f for f in parse_diff(diff) if f.path == u["path"]]
    base = root / ("evaluation/injected" if u["benchmark"] == "injected" else "evaluation/repos")
    head = base / u["case_id"] / u["path"]
    if not files or not head.exists():
        return None
    finding = Finding(
        path=u["path"],
        line=u["finding_line"],
        category="bug-hunter",
        severity=Severity.MEDIUM,
        title="saved",
        message="saved",
        evidence="saved",
        confidence=1.0,
        source="agent",
        suggestion=Suggestion(
            start_line=u["start_line"],
            end_line=u["end_line"],
            replacement=u["replacement"],
            rationale="saved",
        ),
    )
    accepted = RefactoringEngine().validate(finding, files[0], head.read_text())
    if accepted is None:
        return None
    return {**u, "replacement": accepted.replacement}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="evaluation/scores/suggestions.json")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    if not shutil.which("ruff"):
        raise SystemExit("ruff not on PATH")
    eval_dir = root / "evaluation"
    inj_rows = {}
    for line in (eval_dir / "injection_corpus.jsonl").read_text().splitlines():
        row = json.loads(line)
        inj_rows[row["case_id"]] = row
    uniq = load_unique(eval_dir)
    recs = [score_one(u, root, inj_rows) for u in uniq]

    diffs = {cid: row["diff"] for cid, row in inj_rows.items()}
    for line in (eval_dir / "corpus.jsonl").read_text().split("\n"):
        if not line.strip():
            continue
        row = json.loads(line)
        diffs.setdefault(row["case_id"], row["diff"])
    passed = [v for v in (through_validator(u, root, diffs) for u in uniq) if v is not None]
    after = [score_one(v, root, inj_rows) for v in passed]

    groups: dict[str, list] = {"all": recs, "after_production_validator": after}
    for r in recs:
        groups.setdefault(f"source={r['source_class']}", []).append(r)
        groups.setdefault(f"benchmark={r['benchmark']}", []).append(r)
        groups.setdefault(f"benchmark={r['benchmark']},source={r['source_class']}", []).append(r)
    report = {
        "generated_for": "offline suggestion-quality scoring; no LLM calls",
        "inputs": RESULT_FILES,
        "n_unique_validated_suggestions": len(recs),
        "dedupe_key": "case_id+path+start_line+end_line+replacement",
        "production_validator": {
            "note": (
                "saved runs did not pass suggestions through refactor.py; this replays them "
                "through the current validator (range, re-indent, snippet and whole-file parse)"
            ),
            "accepted": len(after),
            "rejected": len(recs) - len(after),
        },
        "definitions": {
            "applies": "range within file; replacement substitutes exactly lines start..end",
            "whole_file_parses": "ast.parse of the full patched file",
            "resolves_static": (
                "ruff code from rule_id stops firing within +-2 lines of the finding "
                "(only where it fired before); semgrep rules not re-run"
            ),
            "hits_defect": "injected cases: suggestion range contains the injected defect line",
            "restores_original": (
                "hits_defect and replaced lines equal (whitespace-normalised) the original code"
            ),
            "changes_defect_line": "hits_defect and the defective line text no longer appears",
            "no_collateral": "patched file parses and gains no new ruff E9/F errors",
            "denominators": "each metric's n counts only suggestions where it is applicable",
            "source_class": "static = purely ruff/semgrep finding; agent = any LLM source",
        },
        "groups": {k: summarise(v) for k, v in sorted(groups.items())},
        "suggestions": recs,
    }
    out = root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{len(recs)} unique validated suggestions -> {out}")


if __name__ == "__main__":
    main()
