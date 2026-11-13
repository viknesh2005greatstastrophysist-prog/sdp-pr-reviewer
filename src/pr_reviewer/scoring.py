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
    load_jsonl,
    location_matches,
    match_detail,
    metrics_from_records,
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


def select_cases(cases: list[EvaluationCase], split: str) -> list[EvaluationCase]:
    return cases if split == "all" else [c for c in cases if c.metadata.get("split") == split]


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


def policy_row(metrics) -> dict[str, Any]:
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
    }


def score_report(
    cases: list[EvaluationCase],
    report: EvaluationReport,
    config: FocusConfig,
    *,
    verify: bool,
    subset: str = "actionable",
) -> dict[str, dict[str, Any]]:
    """split -> variant -> metrics row."""
    result: dict[str, dict[str, Any]] = {}
    for split in SPLITS:
        chosen = select_cases(cases, split)
        if not chosen:
            continue
        rows: dict[str, Any] = {}
        for name, (key, transform) in variants(report, config, verify=verify).items():
            metrics, _ = metrics_from_records(
                key, chosen, report.predictions[key], subset=subset, transform=transform
            )
            rows[name] = policy_row(metrics)
        result[split] = rows
    return result


def score_injected_by_category(
    cases: list[EvaluationCase], report: EvaluationReport, config: FocusConfig, *, verify: bool
) -> dict[str, dict[str, dict[str, Any]]]:
    """split -> variant -> injected category -> row (precision uses that category's cases)."""
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for split in ("test", "dev", "all"):
        chosen = select_cases(cases, split)
        out: dict[str, dict[str, Any]] = {}
        for name, (key, transform) in variants(report, config, verify=verify).items():
            per_cat = {}
            for category in sorted({c.metadata["category"] for c in chosen}):
                subset = [c for c in chosen if c.metadata["category"] == category]
                metrics, _ = metrics_from_records(
                    key, subset, report.predictions[key], subset="all", transform=transform
                )
                per_cat[category] = policy_row(metrics)
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


def policy_table(rows: dict[str, Any], key: str) -> str:
    lines = [
        "| Strategy | TP | FP | FN | Precision | Recall | F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in rows.items():
        part = row[key]
        lines.append(
            f"| {name} | {part['tp']} | {part['fp']} | {part['fn']} | {_fmt(part['p'])} | "
            f"{_fmt(part['r'])} | {_fmt(part['f1'])} |"
        )
    return "\n".join(lines)


def extras_table(rows: dict[str, Any]) -> str:
    lines = [
        "| Strategy | Hunk P | Hunk R | Hunk F1 | P@1 | P@3 | P@5 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in rows.items():
        hunk, at_k = row["hunk"], row["precision_at_k"]
        lines.append(
            f"| {name} | {_fmt(hunk['p'])} | {_fmt(hunk['r'])} | {_fmt(hunk['f1'])} | "
            f"{_fmt(at_k.get('p@1'))} | {_fmt(at_k.get('p@3'))} | {_fmt(at_k.get('p@5'))} |"
        )
    return "\n".join(lines)


def health_table(rows: dict[str, Any]) -> str:
    lines = [
        "| Strategy | Cases | Agent runs | Failures first pass | Failures after retries | "
        "Cases retried | Mean latency (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in rows.items():
        lines.append(
            f"| {name} | {row['cases']} | {row['agent_runs']} | {row['initial_agent_failures']} | "
            f"{row['agent_failures']} | {row['cases_retried']} | {row['mean_latency_ms']:.0f} |"
        )
    return "\n".join(lines)


def category_table(rows: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| Strategy | Category | Cases | Detected (location) | Recall | Precision | F1 | "
        "Recall with category |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, per_cat in rows.items():
        for category, row in per_cat.items():
            loc, cat = row["location"], row["category"]
            lines.append(
                f"| {name} | {category} | {row['cases']} | {loc['tp']} | {_fmt(loc['r'])} | "
                f"{_fmt(loc['p'])} | {_fmt(loc['f1'])} | {_fmt(cat['r'])} |"
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
