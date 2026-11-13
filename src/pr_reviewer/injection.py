"""Controlled benchmark: inject known defects into real code, with exact ground truth.

Human review comments are sparse, so they say little about recall. Here each case is a real
Python file taken from the evaluation corpus snapshots; one deterministic, recorded mutation
turns one line (or inserts one line) into a known defect of a known category. The case diff adds
the whole mutated file, and the single expected finding is the injected line. A reviewer that
reports the injected line found the defect; findings elsewhere are unverified (the surrounding
code is real and was not mutated), so precision here is also a lower bound.

Everything is generated from a seed: the same corpus snapshots and seed give byte-identical
cases. Repository code is read as data and never executed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .evaluation import EvaluationCase, load_jsonl
from .models import Finding, Severity

SEED = 20261113
CATEGORIES = ("bug-hunter", "performance", "style", "architecture")
PER_CATEGORY = 15
MIN_LINES, MAX_LINES = 30, 260


@dataclass(frozen=True)
class Injection:
    mutation: str
    category: str
    description: str
    source: str  # the mutated file text
    line: int  # 1-based line of the defect in the mutated file
    before: str  # original text of the changed line ("" for insertions)
    after: str


Mutator = Callable[[str, ast.Module, random.Random], list[Injection]]


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and not node.name.startswith("__")
    ]


def _first_param(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    names = [a.arg for a in fn.args.posonlyargs + fn.args.args if a.arg not in {"self", "cls"}]
    return names[0] if names else None


def _body_start(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """0-based index of the line where an inserted statement goes (after any docstring)."""
    first = fn.body[0]
    is_doc = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )
    if is_doc and len(fn.body) > 1:
        return fn.body[1].lineno - 1
    if is_doc:
        return first.end_lineno or first.lineno  # after the docstring, same indent
    return first.lineno - 1


def _insert(
    source: str, at: int, lines: list[str], defect_offset: int, indent: str
) -> tuple[str, int, str]:
    rows = source.split("\n")
    block = [indent + text for text in lines]
    rows[at:at] = block
    return "\n".join(rows), at + defect_offset + 1, block[defect_offset].strip()


def _single_line(node: ast.AST) -> bool:
    return getattr(node, "lineno", 0) == getattr(node, "end_lineno", -1)


def _replace_span(source: str, line: int, start: int, end: int, text: str) -> tuple[str, str, str]:
    rows = source.split("\n")
    before = rows[line - 1]
    rows[line - 1] = before[:start] + text + before[end:]
    return "\n".join(rows), before.strip(), rows[line - 1].strip()


# ---- bug-hunter ---------------------------------------------------------------------------


def off_by_one(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "range"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
            and node.args[0].func.id == "len"
            and _single_line(node)
        ):
            arg = node.args[0]
            new, before, after = _replace_span(
                source, node.lineno, arg.end_col_offset or 0, arg.end_col_offset or 0, " + 1"
            )
            found.append(
                Injection(
                    "off-by-one",
                    "bug-hunter",
                    "range(len(x)) extended to range(len(x) + 1): indexes one past the end",
                    new,
                    node.lineno,
                    before,
                    after,
                )
            )
    return found


def wrong_comparison(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    flips = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.Gt: "<=", ast.IsNot: "is"}
    tokens = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.Gt: ">", ast.IsNot: "is not"}
    found = []
    rows = source.split("\n")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If | ast.While) and isinstance(node.test, ast.Compare)):
            continue
        test = node.test
        if len(test.ops) != 1 or type(test.ops[0]) not in flips or not _single_line(test):
            continue
        op = type(test.ops[0])
        left_end = test.left.end_col_offset or 0
        right_start = test.comparators[0].col_offset
        row = rows[test.lineno - 1]
        if row[left_end:right_start].strip() != tokens[op]:
            continue
        gap_start = left_end + (
            len(row[left_end:right_start]) - len(row[left_end:right_start].lstrip())
        )
        gap_end = gap_start + len(tokens[op])
        new, before, after = _replace_span(source, test.lineno, gap_start, gap_end, flips[op])
        found.append(
            Injection(
                "wrong-comparison",
                "bug-hunter",
                f"condition '{tokens[op]}' inverted to '{flips[op]}'",
                new,
                test.lineno,
                before,
                after,
            )
        )
    return found


def missing_none_check(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    found = []
    for fn in _functions(tree):
        args = fn.args.posonlyargs + fn.args.args
        defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
        optional = [
            a.arg
            for a, d in zip(args, defaults, strict=True)
            if isinstance(d, ast.Constant) and d.value is None
        ]
        if not optional or fn.lineno == (fn.body[0].lineno):
            continue
        name = optional[0]
        rows = source.split("\n")
        at = _body_start(fn)
        indent = _indent(rows[fn.body[0].lineno - 1])
        new, line, after = _insert(
            source, at, [f"{name}_normalised = {name}.strip().lower()"], 0, indent
        )
        found.append(
            Injection(
                "missing-none-check",
                "bug-hunter",
                f"'{name}' defaults to None but is dereferenced without a check",
                new,
                line,
                "",
                after,
            )
        )
    return found


def swallowed_exception(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    found = []
    rows = source.split("\n")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ExceptHandler)
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Raise | ast.Return)
            and _single_line(node.body[0])
        ):
            stmt = node.body[0]
            row = rows[stmt.lineno - 1]
            new, before, after = _replace_span(
                source, stmt.lineno, len(_indent(row)), len(row), "pass"
            )
            found.append(
                Injection(
                    "swallowed-exception",
                    "bug-hunter",
                    "exception handler body replaced by 'pass': the error is silently dropped",
                    new,
                    stmt.lineno,
                    before,
                    after,
                )
            )
    return found


def _insert_in_function(
    source: str,
    tree: ast.Module,
    lines_for: Callable[[str], list[str]],
    defect_offset: int,
    mutation: str,
    category: str,
    description: str,
) -> list[Injection]:
    found = []
    rows = source.split("\n")
    for fn in _functions(tree):
        param = _first_param(fn)
        if param is None or fn.body[0].lineno == fn.lineno:
            continue
        indent = _indent(rows[fn.body[0].lineno - 1])
        at = _body_start(fn)
        new, line, after = _insert(source, at, lines_for(param), defect_offset, indent)
        found.append(Injection(mutation, category, description, new, line, "", after))
    return found


def command_injection(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: ["import subprocess", f'subprocess.run("ls " + str({p}), shell=True)'],
        1,
        "command-injection",
        "bug-hunter",
        "argument concatenated into a shell command (shell=True)",
    )


def sql_injection(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: [
            "import sqlite3",
            f"""sqlite3.connect("app.db").execute(f"SELECT * FROM users WHERE name = '{{{p}}}'")""",  # noqa: S608
        ],
        1,
        "sql-injection",
        "bug-hunter",
        "argument interpolated into an SQL string",
    )


def eval_of_input(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: [f"parsed_{p} = eval(str({p}))"],
        0,
        "eval-of-input",
        "bug-hunter",
        "argument passed to eval()",
    )


# ---- performance --------------------------------------------------------------------------


def n_plus_one(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: [
            "import sqlite3",
            f"for _item in {p}:",
            '    sqlite3.connect("app.db").execute("SELECT * FROM t WHERE id = ?", (_item,))',
        ],
        2,
        "n-plus-one-query",
        "performance",
        "one database query (and connection) per element of the input",
    )


def quadratic_loop(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: [f"duplicates = [x for x in {p} if list({p}).count(x) > 1]"],
        0,
        "quadratic-loop",
        "performance",
        "list.count inside a comprehension over the same list: O(n^2)",
    )


def regex_compile_in_loop(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: [
            "import re",
            f"for _x in {p}:",
            '    re.compile(r"^[a-z]+(\\d+)$").match(str(_x))',
        ],
        2,
        "regex-compiled-in-loop",
        "performance",
        "regular expression recompiled on every iteration",
    )


def string_concat_in_loop(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: ['joined = ""', f"for _x in {p}:", "    joined = joined + str(_x) + ','"],
        2,
        "string-concat-in-loop",
        "performance",
        "string rebuilt by repeated concatenation inside a loop",
    )


# ---- style --------------------------------------------------------------------------------


def mutable_default(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    found = []
    rows = source.split("\n")
    for fn in _functions(tree):
        for default in fn.args.defaults + [d for d in fn.args.kw_defaults if d is not None]:
            if (
                isinstance(default, ast.Constant)
                and default.value is None
                and default.lineno == default.end_lineno
            ):
                row = rows[default.lineno - 1]
                new, before, after = _replace_span(
                    source, default.lineno, default.col_offset, default.end_col_offset or 0, "[]"
                )
                found.append(
                    Injection(
                        "mutable-default-argument",
                        "style",
                        "default None replaced by [] (shared between calls)",
                        new,
                        default.lineno,
                        before,
                        after,
                    )
                )
                del row
    return found


def wildcard_import(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    if not imports:
        return []
    last = imports[-1]
    at = last.end_lineno or last.lineno
    new, line, after = _insert(source, at, ["from os.path import *"], 0, "")
    return [
        Injection(
            "wildcard-import",
            "style",
            "wildcard import obscures the module namespace",
            new,
            line,
            "",
            after,
        )
    ]


def equals_true(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: [f"if {p} == True:", "    pass"],
        0,
        "comparison-to-true",
        "style",
        "comparison to True with == instead of truthiness / identity",
    )


def type_equality(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: [f"if type({p}) == str:", "    pass"],
        0,
        "type-equality",
        "style",
        "type comparison with == instead of isinstance",
    )


# ---- architecture -------------------------------------------------------------------------


def global_state(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    if not imports:
        return []
    base = _insert(
        source, (imports[-1].end_lineno or imports[-1].lineno), ["", "_REQUEST_STATE = {}"], 1, ""
    )[0]
    shifted = ast.parse(base)
    found = _insert_in_function(
        base,
        shifted,
        lambda p: [f'_REQUEST_STATE["last"] = {p}'],
        0,
        "process-global-state",
        "architecture",
        "function writes to a module-level mutable dict shared by every request",
    )
    return found


def hardcoded_endpoint(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: [f'endpoint = "http://10.0.0.5:8080/internal/v1/" + str({p})'],
        0,
        "hard-coded-endpoint",
        "architecture",
        "internal service address hard-coded instead of configured",
    )


def layering_violation(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    return _insert_in_function(
        source,
        tree,
        lambda p: ["from django.http import HttpResponse as _Response"],
        0,
        "layering-violation",
        "architecture",
        "library function imports a web framework: couples a low-level module to a high-level one",
    )


def breaking_signature(source: str, tree: ast.Module, rng: random.Random) -> list[Injection]:
    found = []
    rows = source.split("\n")
    for fn in _functions(tree):
        if fn.name.startswith("_") or not fn.args.defaults:
            continue
        positional = fn.args.posonlyargs + fn.args.args
        first_default_arg = positional[len(positional) - len(fn.args.defaults)]
        default = fn.args.defaults[0]
        if (
            first_default_arg.end_lineno != default.end_lineno
            or default.lineno != default.end_lineno
        ):
            continue
        row = rows[default.lineno - 1]
        start = first_default_arg.end_col_offset or 0
        new, before, after = _replace_span(
            source, default.lineno, start, default.end_col_offset or 0, ""
        )
        del row
        found.append(
            Injection(
                "removed-default-argument",
                "architecture",
                "public parameter made mandatory: breaks existing callers",
                new,
                default.lineno,
                before,
                after,
            )
        )
    return found


MUTATORS: dict[str, list[Mutator]] = {
    "bug-hunter": [
        off_by_one,
        wrong_comparison,
        missing_none_check,
        swallowed_exception,
        command_injection,
        sql_injection,
        eval_of_input,
    ],
    "performance": [n_plus_one, quadratic_loop, regex_compile_in_loop, string_concat_in_loop],
    "style": [mutable_default, wildcard_import, equals_true, type_equality],
    "architecture": [global_state, hardcoded_endpoint, layering_violation, breaking_signature],
}


def candidate_files(corpus: list[EvaluationCase]) -> list[tuple[str, str, str, Path]]:
    """(repo, split, relative path, file) for non-test source files of suitable size, deduped."""
    seen: set[tuple[str, str]] = set()
    found = []
    for case in corpus:
        root = Path(case.repo_path or "")
        if not root.is_dir():
            continue
        for file in sorted(root.rglob("*.py")):
            relative = file.relative_to(root).as_posix()
            key = (case.metadata["repo"], relative)
            parts = relative.lower().split("/")
            if key in seen or any("test" in p or p in {"docs", "scripts"} for p in parts):
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
            if not MIN_LINES <= text.count("\n") <= MAX_LINES:
                continue
            try:
                ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            seen.add(key)
            found.append((case.metadata["repo"], case.metadata.get("split") or "", relative, file))
    return found


def new_file_diff(path: str, text: str) -> str:
    body = text.rstrip("\n").split("\n")
    header = (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(body)} @@\n"
    )
    return header + "".join(f"+{row}\n" for row in body)


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return True


def generate(
    corpus: list[EvaluationCase],
    output_root: Path,
    *,
    seed: int = SEED,
    per_category: int = PER_CATEGORY,
) -> list[EvaluationCase]:
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not security
    files = candidate_files(corpus)
    rng.shuffle(files)
    used_files: set[tuple[str, str]] = set()
    cases: list[EvaluationCase] = []
    repo_cap = -(-per_category // 3)
    for category in CATEGORIES:
        mutators = MUTATORS[category]
        picked = 0
        repo_counts: Counter[str] = Counter()
        mutation_counts: Counter[str] = Counter()
        # First pass spreads cases over repositories; the second fills any shortfall.
        for cap in (repo_cap, per_category):
            for repo, split, relative, file in files:
                if picked >= per_category:
                    break
                if (repo, relative) in used_files or repo_counts[repo] >= cap:
                    continue
                text = file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
                chosen = None
                # Least-used mutation kinds first so every kind appears.
                for mutator in sorted(
                    mutators, key=lambda m: (mutation_counts[m.__name__], rng.random())
                ):
                    options = [o for o in mutator(text, tree, rng) if _parses(o.source)]
                    if options:
                        chosen = rng.choice(options)
                        mutation_counts[mutator.__name__] += 1
                        break
                if chosen is None:
                    continue
                used_files.add((repo, relative))
                repo_counts[repo] += 1
                picked += 1
                case_id = f"inj__{category}__{picked:02d}__{repo.replace('/', '_')}"
                destination = output_root / case_id
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(chosen.source, encoding="utf-8")
                expected = Finding(
                    path=relative,
                    line=chosen.line,
                    category=category,
                    severity=Severity.MEDIUM,
                    title=chosen.mutation,
                    message=chosen.description,
                    evidence=f"injected by {chosen.mutation} at line {chosen.line}: {chosen.after}",
                    source="injected",
                    confidence=1.0,
                )
                cases.append(
                    EvaluationCase(
                        case_id=case_id,
                        diff=new_file_diff(relative, chosen.source),
                        human_findings=[expected],
                        repo_path=str(destination),
                        metadata={
                            "repo": repo,
                            "split": split,
                            "benchmark": "injected-defects",
                            "mutation": chosen.mutation,
                            "category": category,
                            "injected_line": chosen.line,
                            "line_before": chosen.before,
                            "line_after": chosen.after,
                            "seed": seed,
                            "source_file": relative,
                        },
                    )
                )
    return cases


def card(cases: list[EvaluationCase], sha256: str) -> str:
    by_cat = Counter(c.metadata["category"] for c in cases)
    by_mut = Counter((c.metadata["category"], c.metadata["mutation"]) for c in cases)
    by_split = Counter(c.metadata["split"] for c in cases)
    lines = [
        "# Injected-defect benchmark card",
        "",
        f"- Cases: {len(cases)} (seed {cases[0].metadata['seed'] if cases else '-'}); "
        f"corpus SHA-256 (evaluation/injection_corpus.jsonl): `{sha256}`",
        "- Each case adds one real source file from the evaluation corpus with exactly one "
        "deterministic injected defect; ground truth = the injected line (recorded in metadata).",
        f"- Split inherited from the source repository: {dict(by_split)}",
        "",
        "| Category | Mutation | Cases |",
        "| --- | --- | ---: |",
    ]
    lines += [f"| {c} | {m} | {n} |" for (c, m), n in sorted(by_mut.items())]
    lines += ["", "| Category | Cases |", "| --- | ---: |"]
    lines += [f"| {c} | {by_cat[c]} |" for c in CATEGORIES]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the injected-defect benchmark")
    parser.add_argument("--corpus", type=Path, default=Path("evaluation/corpus.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("evaluation/injection_corpus.jsonl"))
    parser.add_argument("--root", type=Path, default=Path("evaluation/injected"))
    parser.add_argument("--card", type=Path, default=Path("evaluation/INJECTION.md"))
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--per-category", type=int, default=PER_CATEGORY)
    args = parser.parse_args()
    cases = generate(
        load_jsonl(args.corpus), args.root, seed=args.seed, per_category=args.per_category
    )
    rows = "".join(case.model_dump_json() + "\n" for case in cases)
    args.output.write_text(rows, encoding="utf-8")
    digest = hashlib.sha256(rows.encode("utf-8")).hexdigest()
    args.card.write_text(card(cases, digest), encoding="utf-8")
    print(f"Wrote {len(cases)} injected case(s) to {args.output} (sha256 {digest})")


if __name__ == "__main__":
    main()
