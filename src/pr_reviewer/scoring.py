"""Offline scoring of saved predictions: dev-only tuning, split tables, adjudication sample.

Nothing here calls an LLM. The evaluation runner saves each strategy's full (unfiltered)
predictions per case; this module replays the focused review mode on them, so a configuration
can be chosen on the DEV split and then reported on the TEST split without touching the model.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import random
from collections import defaultdict
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .evaluation import (
    CaseRecord,
    EvaluationCase,
    EvaluationReport,
    ground_truth,
    hunk_policy,
    load_jsonl,
    location_matches,
    match_detail,
    matches,
    metrics_from_records,
    prf,
)
from .models import Finding
from .review_mode import (
    COSMETIC_RULES,
    STATIC_SOURCES,
    FocusConfig,
    apply_review_mode,
    static_rule_kept,
)
from .verifier import drop_ungrounded

SPLITS = ("dev", "test", "all")
STRATEGY_ORDER = ("static-only", "single-agent", "hybrid-multi-agent")
ADJUDICATION_SEED = 20261113
ADJUDICATION_SIZE = 50


def focus_transform(record: CaseRecord, config: FocusConfig, *, verify: bool):
    """Findings the focused review mode would show for a saved prediction."""
    findings = record.findings
    if verify and record.verified and not record.verify_error:
        findings, _ = drop_ungrounded(findings, record.verdicts)
    return apply_review_mode(findings, "focused", config)[0]


BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 20261114
POLICY_FACTORIES = {
    "location": lambda case: location_matches,
    "category": lambda case: matches,
    "hunk": hunk_policy,
}


def case_counts(
    cases: list[EvaluationCase],
    records: dict[str, CaseRecord],
    policy: str,
    *,
    subset: str,
    transform=None,
) -> list[tuple[int, int, int]]:
    """Per-case (tp, fp, fn) under one policy: the unit that the bootstrap resamples."""
    counts = []
    for case in cases:
        record = records[case.case_id]
        findings = transform(record) if transform else record.findings
        matched, false_pos, missed = match_detail(
            findings, ground_truth(case, subset), POLICY_FACTORIES[policy](case)
        )
        counts.append((len(matched), len(false_pos), len(missed)))
    return counts


def draw_indices(n: int, draws: int = BOOTSTRAP_DRAWS, seed: int = BOOTSTRAP_SEED):
    rng = random.Random(seed)  # noqa: S311 - reproducible resampling, not security
    return [[rng.randrange(n) for _ in range(n)] for _ in range(draws)]


def _prf_of(counts, picks) -> tuple[float, float, float]:
    tp = sum(counts[i][0] for i in picks)
    fp = sum(counts[i][1] for i in picks)
    fn = sum(counts[i][2] for i in picks)
    result = prf(tp, fp, fn)
    return result.precision, result.recall, result.f1


def _interval(values: list[float]) -> list[float]:
    ordered = sorted(values)
    return [
        ordered[int(0.025 * (len(ordered) - 1))],
        ordered[int(0.975 * (len(ordered) - 1) + 0.5)],
    ]


def bootstrap_ci(counts: list[tuple[int, int, int]], indices=None) -> dict[str, Any]:
    """95% percentile bootstrap over cases (2000 draws, fixed seed) for micro P/R/F1."""
    if not counts:
        return {"n_cases": 0}
    indices = indices or draw_indices(len(counts))
    draws = [_prf_of(counts, picks) for picks in indices]
    return {
        "n_cases": len(counts),
        "draws": len(indices),
        "seed": BOOTSTRAP_SEED,
        **{
            name: _interval([d[position] for d in draws])
            for position, name in enumerate(("p", "r", "f1"))
        },
    }


def paired_difference(counts_a, counts_b, indices=None) -> dict[str, Any]:
    """A minus B on the same resampled cases; says whether the interval includes zero."""
    if not counts_a or len(counts_a) != len(counts_b):
        return {"n_cases": 0}
    indices = indices or draw_indices(len(counts_a))
    point_a, point_b = (
        _prf_of(counts_a, range(len(counts_a))),
        _prf_of(counts_b, range(len(counts_b))),
    )
    deltas = [
        [x - y for x, y in zip(_prf_of(counts_a, picks), _prf_of(counts_b, picks), strict=True)]
        for picks in indices
    ]
    out: dict[str, Any] = {"n_cases": len(counts_a), "draws": len(indices)}
    for position, name in enumerate(("p", "r", "f1")):
        low, high = _interval([d[position] for d in deltas])
        out[name] = {
            "diff": point_a[position] - point_b[position],
            "ci": [low, high],
            "includes_zero": low <= 0 <= high,
        }
    return out


PAIRS = (
    ("hybrid (full)", "single-agent"),
    ("hybrid (full)", "static-only"),
    ("hybrid (focused)", "single-agent (focused)"),
    ("hybrid (focused)", "static-only (focused)"),
    ("hybrid (focused)", "hybrid (full)"),
)


def select_cases(cases: list[EvaluationCase], split: str) -> list[EvaluationCase]:
    return cases if split == "all" else [c for c in cases if c.metadata.get("split") == split]


def completed(cases: list[EvaluationCase], report: EvaluationReport) -> list[EvaluationCase]:
    """Cases every strategy of the report has a prediction for (runs can stop early on quota)."""
    return [c for c in cases if all(c.case_id in recs for recs in report.predictions.values())]


def variants(
    report: EvaluationReport, config: FocusConfig, *, verify: bool
) -> dict[str, tuple[str, Any]]:
    """Display name -> (strategy key in the report, transform or None)."""
    named = {
        "static-only": "static-only",
        "single-agent": "single-agent",
        "hybrid-multi-agent": "hybrid (full)",
    }
    out: dict[str, tuple[str, Any]] = {}
    for key in STRATEGY_ORDER:
        if key not in report.predictions:
            continue
        out[named[key]] = (key, None)
    for key in STRATEGY_ORDER:
        if key not in report.predictions:
            continue
        use_verify = verify and key != "static-only"
        label = {
            "static-only": "static-only (focused)",
            "single-agent": "single-agent (focused)",
            "hybrid-multi-agent": "hybrid (focused)",
        }[key]
        out[label] = (
            key,
            lambda rec, use=use_verify: focus_transform(rec, config, verify=use),
        )
    return out


def policy_row(metrics, ci: dict[str, Any] | None = None) -> dict[str, Any]:
    def triple(p):
        return None if p is None else {"p": p.precision, "r": p.recall, "f1": p.f1}

    return {
        "cases": metrics.cases,
        "location": {
            **triple(metrics.location_only),
            "tp": metrics.location_only.true_positives,
            "fp": metrics.location_only.false_positives,
            "fn": metrics.location_only.false_negatives,
        },
        "category": {
            "p": metrics.precision,
            "r": metrics.recall,
            "f1": metrics.f1,
            "tp": metrics.true_positives,
            "fp": metrics.false_positives,
            "fn": metrics.false_negatives,
        },
        "hunk": triple(metrics.hunk_level),
        "precision_at_k": metrics.precision_at_k,
        "agent_runs": metrics.agent_runs,
        "agent_failures": metrics.agent_failures,
        "initial_agent_failures": metrics.initial_agent_failures,
        "cases_retried": metrics.cases_retried,
        "mean_latency_ms": metrics.mean_latency_ms,
        "ci": ci or {},
    }


def _row(chosen, key, records, transform, subset) -> tuple[dict[str, Any], dict[str, list]]:
    metrics, _ = metrics_from_records(key, chosen, records, subset=subset, transform=transform)
    counts = {
        policy: case_counts(chosen, records, policy, subset=subset, transform=transform)
        for policy in POLICY_FACTORIES
    }
    indices = draw_indices(len(chosen)) if chosen else []
    ci = {policy: bootstrap_ci(c, indices) for policy, c in counts.items()}
    return policy_row(metrics, ci), counts


def score_report(
    cases: list[EvaluationCase],
    report: EvaluationReport,
    config: FocusConfig,
    *,
    verify: bool,
    subset: str = "actionable",
) -> dict[str, dict[str, Any]]:
    """split -> variant -> metrics row (with bootstrap CIs); split -> "_paired" -> comparisons."""
    result: dict[str, dict[str, Any]] = {}
    cases = completed(cases, report)
    for split in SPLITS:
        chosen = select_cases(cases, split)
        if not chosen:
            continue
        rows: dict[str, Any] = {}
        all_counts: dict[str, dict[str, list]] = {}
        for name, (key, transform) in variants(report, config, verify=verify).items():
            rows[name], all_counts[name] = _row(
                chosen, key, report.predictions[key], transform, subset
            )
        indices = draw_indices(len(chosen))
        paired = {}
        for left, right in PAIRS:
            if left in all_counts and right in all_counts:
                paired[f"{left} minus {right}"] = {
                    policy: paired_difference(
                        all_counts[left][policy], all_counts[right][policy], indices
                    )
                    for policy in POLICY_FACTORIES
                }
        result[split] = {**rows, "_paired": paired}
    return result


def score_injected_by_category(
    cases: list[EvaluationCase], report: EvaluationReport, config: FocusConfig, *, verify: bool
) -> dict[str, dict[str, dict[str, Any]]]:
    """split -> variant -> injected category -> row (precision uses that category's cases)."""
    result: dict[str, dict[str, dict[str, Any]]] = {}
    cases = completed(cases, report)
    for split in ("test", "dev", "all"):
        chosen = select_cases(cases, split)
        out: dict[str, dict[str, Any]] = {}
        for name, (key, transform) in variants(report, config, verify=verify).items():
            per_cat = {}
            for category in sorted({c.metadata["category"] for c in chosen}):
                subset = [c for c in chosen if c.metadata["category"] == category]
                per_cat[category], _ = _row(subset, key, report.predictions[key], transform, "all")
            out[name] = per_cat
        result[split] = out
    return result


# ---- dev-only tuning ----------------------------------------------------------------------


def rule_statistics(
    cases: list[EvaluationCase], report: EvaluationReport, strategy: str
) -> dict[str, dict[str, int]]:
    """Per static rule: how often it fired on the cases, and how often it hit a human comment."""
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"fired": 0, "hit": 0})
    for case in cases:
        expected = ground_truth(case, "actionable")
        for finding in report.predictions[strategy][case.case_id].findings:
            if finding.source not in STATIC_SOURCES:
                continue
            code = finding.rule_id or finding.title
            stats[code]["fired"] += 1
            stats[code]["hit"] += any(location_matches(finding, e) for e in expected)
    return dict(stats)


def prune_candidates(
    stats: dict[str, dict[str, int]], config: FocusConfig, min_fired: int = 5
) -> list[str]:
    """Rules the principled list keeps but that fired often on DEV and never hit a comment."""
    candidates = []
    for code, entry in stats.items():
        probe = Finding(
            path="x",
            line=1,
            category="style",
            severity="LOW",
            message="m",
            evidence="e",
            source="ruff",
            rule_id=code,
        )
        if (
            entry["fired"] >= min_fired
            and entry["hit"] == 0
            and static_rule_kept(probe, config)
            and not code.startswith("python-")  # custom Semgrep rules stay
        ):
            candidates.append(code)
    return sorted(candidates)


GRID = {
    "agent_min_confidence": (0.0, 0.5, 0.6, 0.7, 0.8, 0.9),
    "max_per_file": (2, 3, 5, 99),
    "max_per_pr": (None, 3, 5, 8),
    "verify": (False, True),
}


def f1_of(cases, report, strategy, config, verify) -> float:
    metrics, _ = metrics_from_records(
        strategy,
        cases,
        report.predictions[strategy],
        subset="actionable",
        transform=lambda rec: focus_transform(rec, config, verify=verify),
    )
    return metrics.location_only.f1 if metrics.location_only else 0.0


def tune(
    benchmarks: list[tuple[str, list[EvaluationCase], EvaluationReport]],
    strategy: str = "hybrid-multi-agent",
) -> dict[str, Any]:
    """Pick pruned rules and thresholds on the dev split(s) only (mean location-only F1)."""
    dev = [(name, select_cases(cases, "dev"), report) for name, cases, report in benchmarks]
    base = FocusConfig()
    stats: dict[str, dict[str, int]] = {}
    for _, cases, report in dev:
        if "static-only" in report.predictions:
            for code, entry in rule_statistics(cases, report, "static-only").items():
                total = stats.setdefault(code, {"fired": 0, "hit": 0})
                total["fired"] += entry["fired"]
                total["hit"] += entry["hit"]
    pruned = prune_candidates(stats, base)
    base = replace(base, cosmetic=frozenset(COSMETIC_RULES | set(pruned)))
    results = []
    names = list(GRID)
    for values in itertools.product(*(GRID[n] for n in names)):
        choice = dict(zip(names, values, strict=True))
        config = replace(
            base,
            agent_min_confidence=choice["agent_min_confidence"],
            max_per_file=choice["max_per_file"],
            max_per_pr=choice["max_per_pr"],
        )
        scores = [
            f1_of(cases, report, strategy, config, choice["verify"])
            for _, cases, report in dev
            if strategy in report.predictions
        ]
        results.append({**choice, "mean_f1": sum(scores) / len(scores), "f1_by_benchmark": scores})
    # Best mean F1; ties go to the simplest setting (no cap, no verification, lowest threshold).
    results.sort(
        key=lambda r: (
            -round(r["mean_f1"], 6),
            r["max_per_pr"] is not None,
            r["verify"],
            r["agent_min_confidence"],
        )
    )
    best = results[0]
    return {
        "tuned_on": "dev split only",
        "benchmarks": [name for name, _, _ in dev],
        "strategy": strategy,
        "objective": "mean over benchmarks of location-only F1 on actionable ground truth",
        "rule_statistics_dev": dict(sorted(stats.items(), key=lambda kv: -kv[1]["fired"])),
        "pruned_rules": pruned,
        "best": best,
        "top10": results[:10],
        "untuned_baseline_f1": [
            f1_of(c, r, strategy, FocusConfig(), False)
            for _, c, r in dev
            if strategy in r.predictions
        ],
        "grid_size": len(results),
    }


# ---- adjudication sample ------------------------------------------------------------------


def adjudication_rows(
    cases: list[EvaluationCase],
    report: EvaluationReport,
    config: FocusConfig,
    *,
    verify: bool,
    strategy: str = "hybrid-multi-agent",
    size: int = ADJUDICATION_SIZE,
    seed: int = ADJUDICATION_SEED,
) -> list[dict[str, Any]]:
    """Seeded random sample of the focused hybrid's unmatched findings on the test split."""
    pool = []
    for case in select_cases(cases, "test"):
        record = report.predictions[strategy][case.case_id]
        findings = focus_transform(record, config, verify=verify)
        _, false_positives, _ = match_detail(
            findings, ground_truth(case, "actionable"), location_matches
        )
        for finding in false_positives:
            pool.append((case, finding))
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not security
    sample = rng.sample(pool, min(size, len(pool)))
    return [
        {
            "case_id": case.case_id,
            "repo": case.metadata.get("repo", ""),
            "file": finding.path,
            "line": finding.line,
            "category": finding.category,
            "severity": finding.severity.value,
            "confidence": finding.confidence,
            "source": finding.source,
            "finding": f"{finding.title}: {finding.message}"[:700],
            "evidence": finding.evidence[:700],
            "team_valid": "",
            "notes": "",
        }
        for case, finding in sample
    ] + [{"_pool": len(pool)}]


def write_adjudication(rows: list[dict[str, Any]], output: Path) -> int:
    rows = [r for r in rows if "_pool" not in r]
    columns = [
        "case_id",
        "repo",
        "file",
        "line",
        "category",
        "severity",
        "confidence",
        "source",
        "finding",
        "evidence",
        "team_valid",
        "notes",
    ]
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ---- markdown -----------------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _ci(row: dict[str, Any], policy: str, name: str, value: float | None) -> str:
    interval = row.get("ci", {}).get(policy, {}).get(name)
    if not interval:
        return _fmt(value)
    return f"{_fmt(value)} [{interval[0]:.2f}, {interval[1]:.2f}]"


def _visible(rows: dict[str, Any]):
    return ((name, row) for name, row in rows.items() if not name.startswith("_"))


def policy_table(rows: dict[str, Any], key: str) -> str:
    lines = [
        "| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | "
        "F1 (95% CI) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in _visible(rows):
        part = row[key]
        lines.append(
            f"| {name} | {row['cases']} | {part['tp']} | {part['fp']} | {part['fn']} | "
            f"{_ci(row, key, 'p', part['p'])} | {_ci(row, key, 'r', part['r'])} | "
            f"{_ci(row, key, 'f1', part['f1'])} |"
        )
    return "\n".join(lines)


def extras_table(rows: dict[str, Any]) -> str:
    lines = [
        "| Strategy | Hunk P (95% CI) | Hunk R (95% CI) | Hunk F1 (95% CI) | P@1 | P@3 | P@5 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in _visible(rows):
        hunk, at_k = row["hunk"], row["precision_at_k"]
        lines.append(
            f"| {name} | {_ci(row, 'hunk', 'p', hunk['p'])} | {_ci(row, 'hunk', 'r', hunk['r'])} | "
            f"{_ci(row, 'hunk', 'f1', hunk['f1'])} | {_fmt(at_k.get('p@1'))} | "
            f"{_fmt(at_k.get('p@3'))} | {_fmt(at_k.get('p@5'))} |"
        )
    return "\n".join(lines)


def _delta(entry: dict[str, Any]) -> str:
    return f"{entry['diff']:+.3f} [{entry['ci'][0]:+.2f}, {entry['ci'][1]:+.2f}]"


def paired_table(rows: dict[str, Any], policy: str = "location") -> str:
    lines = [
        "| Comparison (A minus B) | n cases | dP (95% CI) | dR (95% CI) | dF1 (95% CI) | "
        "F1 interval includes 0? |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for name, by_policy in rows.get("_paired", {}).items():
        part = by_policy[policy]

        verdict = "yes: not distinguishable from 0" if part["f1"]["includes_zero"] else "no"
        lines.append(
            f"| {name} | {part['n_cases']} | {_delta(part['p'])} | {_delta(part['r'])} | "
            f"{_delta(part['f1'])} | {verdict} |"
        )
    return "\n".join(lines)


def health_table(rows: dict[str, Any]) -> str:
    lines = [
        "| Strategy | Cases | Agent runs | Failures first pass | Failures after retries | "
        "Cases retried | Mean latency (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in _visible(rows):
        lines.append(
            f"| {name} | {row['cases']} | {row['agent_runs']} | {row['initial_agent_failures']} | "
            f"{row['agent_failures']} | {row['cases_retried']} | {row['mean_latency_ms']:.0f} |"
        )
    return "\n".join(lines)


def category_table(rows: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| Strategy | Category | Cases | Detected (location) | Recall (95% CI) | "
        "Precision (95% CI) | F1 (95% CI) | Recall with category |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, per_cat in _visible(rows):
        for category, row in per_cat.items():
            loc, cat = row["location"], row["category"]
            lines.append(
                f"| {name} | {category} | {row['cases']} | {loc['tp']} | "
                f"{_ci(row, 'location', 'r', loc['r'])} | {_ci(row, 'location', 'p', loc['p'])} | "
                f"{_ci(row, 'location', 'f1', loc['f1'])} | {_fmt(cat['r'])} |"
            )
    return "\n".join(lines)


# ---- command line -------------------------------------------------------------------------


def _load(corpus: Path, results: Path) -> tuple[list[EvaluationCase], EvaluationReport]:
    return load_jsonl(corpus), EvaluationReport.model_validate_json(
        results.read_text(encoding="utf-8")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Score saved evaluation predictions offline")
    sub = parser.add_subparsers(dest="command", required=True)
    tune_cmd = sub.add_parser("tune", help="choose focused-mode settings on the dev split")
    tune_cmd.add_argument("--human", nargs=2, metavar=("CORPUS", "RESULTS"), required=True)
    tune_cmd.add_argument("--injected", nargs=2, metavar=("CORPUS", "RESULTS"))
    tune_cmd.add_argument("--output", type=Path, required=True)
    score_cmd = sub.add_parser("score", help="write split tables for the current defaults")
    score_cmd.add_argument("--human", nargs=2, metavar=("CORPUS", "RESULTS"), required=True)
    score_cmd.add_argument("--injected", nargs=2, metavar=("CORPUS", "RESULTS"))
    score_cmd.add_argument("--output", type=Path, required=True)
    score_cmd.add_argument("--adjudication", type=Path)
    score_cmd.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()

    benchmarks = [("human-comments", *_load(*map(Path, args.human)))]
    if args.injected:
        benchmarks.append(("injected-defects", *_load(*map(Path, args.injected))))
    if args.command == "tune":
        args.output.write_text(json.dumps(tune(benchmarks), indent=2), encoding="utf-8")
        print(f"Wrote dev tuning to {args.output}")
        return
    from .settings import Settings

    config = FocusConfig(max_per_pr=Settings().review_top_k or None)
    verify = not args.no_verify
    payload: dict[str, Any] = {
        "focus_config": {**asdict(config), "cosmetic": sorted(config.cosmetic), "verify": verify},
        "benchmarks": {},
    }
    for name, cases, report in benchmarks:
        entry: dict[str, Any] = {
            "model": report.environment.get("model_id"),
            "corpus_sha256": report.environment.get("corpus_sha256"),
            "run_config": report.run_config,
            "tables": score_report(cases, report, config, verify=verify),
        }
        if name == "human-comments":
            entry["addressed_subset"] = score_report(
                cases, report, config, verify=verify, subset="addressed"
            )
            entry["cases_in_corpus"] = len(cases)
            entry["cases_completed"] = {
                split: len(select_cases(completed(cases, report), split)) for split in SPLITS
            }
            entry["ground_truth_counts"] = {
                split: {
                    "cases": len(select_cases(cases, split)),
                    "actionable": sum(len(ground_truth(c)) for c in select_cases(cases, split)),
                    "addressed": sum(
                        len(ground_truth(c, "addressed")) for c in select_cases(cases, split)
                    ),
                    "all_comments": sum(len(c.human_findings) for c in select_cases(cases, split)),
                }
                for split in SPLITS
            }
            if args.adjudication:
                rows = adjudication_rows(cases, report, config, verify=verify)
                entry["adjudication_pool"] = rows[-1]["_pool"]
                entry["adjudication_rows"] = write_adjudication(rows, args.adjudication)
        else:
            entry["by_category"] = score_injected_by_category(cases, report, config, verify=verify)
        payload["benchmarks"][name] = entry
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote scores to {args.output}")


if __name__ == "__main__":
    main()
