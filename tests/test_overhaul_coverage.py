"""Network-free tests for scoring, labelling, injection, evaluation and CLI entry points."""

import ast
import asyncio
import csv
import json
import random
import sys
from pathlib import Path

import httpx
import pytest

from pr_reviewer import corpus as corpus_module
from pr_reviewer import evaluation as evaluation_module
from pr_reviewer import injection, labeling, scoring
from pr_reviewer.cli import main as cli_main
from pr_reviewer.evaluation import (
    CaseRecord,
    EvaluationCase,
    EvaluationReport,
    ground_truth,
    load_jsonl,
    verify_record,
)
from pr_reviewer.llm import OpenAICompatibleClient
from pr_reviewer.models import AgentRun, Finding, Severity
from pr_reviewer.parser import parse_diff
from pr_reviewer.review_mode import FocusConfig
from pr_reviewer.verifier import finding_key

ROOT = Path(__file__).resolve().parent.parent


def finding(path="m.py", line=1, category="bug-hunter", source="bug-hunter", confidence=0.9, **kw):
    return Finding(
        path=path,
        line=line,
        category=category,
        severity=Severity.MEDIUM,
        title=kw.pop("title", f"t{line}"),
        message="m",
        evidence="e",
        source=source,
        confidence=confidence,
        **kw,
    )


def success():
    return [AgentRun(agent="bug-hunter", status="success", duration_ms=1, finding_count=0)]


def make_case(case_id, split, expected, labels=None, category="bug-hunter"):
    return EvaluationCase(
        case_id=case_id,
        diff=injection.new_file_diff("m.py", "\n".join(f"x{i} = {i}" for i in range(60)) + "\n"),
        human_findings=expected,
        metadata={
            "repo": "o/r",
            "split": split,
            "category": category,
            "finding_labels": labels or [{"actionable": True} for _ in expected],
        },
    )


# Hand-computed fixture.
#   c1 (dev):  expected line 10; predicted nothing.
#   c2 (test): expected line 20; predicted 21 (conf .9) and 50 (conf .3).
#   c3 (test): expected 30 (NOT actionable) and 40 (actionable); predicted 40 then 30.
def fixture():
    cases = [
        make_case("c1", "dev", [finding(line=10)]),
        make_case("c2", "test", [finding(line=20)]),
        make_case(
            "c3",
            "test",
            [finding(line=30), finding(line=40, category="style")],
            labels=[{"actionable": False}, {"actionable": True, "addressed": True}],
        ),
    ]
    bad = finding(line=30, confidence=0.9, title="ungrounded")
    hybrid = {
        "c1": CaseRecord(agent_runs=success(), verified=True),
        "c2": CaseRecord(
            findings=[finding(line=21), finding(line=50, confidence=0.3)],
            agent_runs=success(),
            verified=True,
        ),
        "c3": CaseRecord(
            findings=[finding(line=40, category="style"), bad],
            agent_runs=success(),
            verified=True,
            verdicts={finding_key(bad): False},
        ),
    }
    static = {cid: CaseRecord() for cid in hybrid}
    report = EvaluationReport(
        corpus_cases=3,
        generated_at_epoch=1,
        matching_policy="p",
        strategies=[],
        false_positive_adjudication={},
        environment={"model_id": "fake-model"},
        predictions={"static-only": static, "hybrid-multi-agent": hybrid},
    )
    return cases, report


CONFIG = FocusConfig(max_per_file=99, max_per_pr=None)


def test_ground_truth_subsets():
    cases, _ = fixture()
    c3 = cases[2]
    assert [f.line for f in ground_truth(c3)] == [40]
    assert [f.line for f in ground_truth(c3, "addressed")] == [40]
    assert [f.line for f in ground_truth(c3, "all")] == [30, 40]
    assert [f.line for f in ground_truth(cases[1], "addressed")] == []


def test_exact_prf_per_split_policy_hunk_and_precision_at_k():
    cases, report = fixture()
    tables = scoring.score_report(cases, report, CONFIG, verify=False)
    assert set(tables) == {"dev", "test", "all"}
    row = tables["test"]["hybrid (full)"]
    # test split: expected {c2:20, c3:40}; matched 21->20 and 40->40; FPs 50 and 30.
    loc = row["location"]
    assert (loc["tp"], loc["fp"], loc["fn"]) == (2, 2, 0)
    assert loc["p"] == 0.5 and loc["r"] == 1.0 and loc["f1"] == pytest.approx(2 / 3)
    # category policy: 21->20 same category; 40 (style) vs expected style also matches.
    cat = row["category"]
    assert (cat["tp"], cat["fp"], cat["fn"]) == (2, 2, 0)
    assert row["hunk"]["r"] == 1.0 and row["hunk"]["p"] == 0.5
    assert row["precision_at_k"]["p@1"] == 1.0
    assert row["precision_at_k"]["p@3"] == 0.5
    dev = tables["dev"]["hybrid (full)"]["location"]
    assert (dev["tp"], dev["fp"], dev["fn"]) == (0, 0, 1) and dev["r"] == 0.0 and dev["f1"] == 0.0
    overall = tables["all"]["hybrid (full)"]["location"]
    assert (overall["tp"], overall["fp"], overall["fn"]) == (2, 2, 1)
    assert overall["r"] == pytest.approx(2 / 3)


def test_category_policy_requires_matching_category():
    case = make_case("k", "test", [finding(line=5, category="style")])
    report = fixture()[1]
    report.predictions["hybrid-multi-agent"]["k"] = CaseRecord(
        findings=[finding(line=5, category="bug-hunter")], agent_runs=success()
    )
    report.predictions["static-only"]["k"] = CaseRecord()
    tables = scoring.score_report([case], report, CONFIG, verify=False)
    row = tables["test"]["hybrid (full)"]
    assert row["location"]["tp"] == 1 and row["category"]["tp"] == 0
    assert row["category"]["fp"] == 1 and row["category"]["fn"] == 1


def test_focused_filter_and_verification_change_precision_only():
    cases, report = fixture()
    plain = scoring.score_report(cases, report, CONFIG, verify=False)["test"]["hybrid (focused)"]
    assert plain["location"]["fp"] == 1 and plain["location"]["tp"] == 2  # conf .3 dropped
    assert plain["location"]["f1"] == pytest.approx(0.8)
    checked = scoring.score_report(cases, report, CONFIG, verify=True)["test"]["hybrid (focused)"]
    assert checked["location"]["fp"] == 0 and checked["location"]["f1"] == 1.0


def test_addressed_subset_scores_only_addressed_comments():
    cases, report = fixture()
    row = scoring.score_report(cases, report, CONFIG, verify=False, subset="addressed")
    loc = row["test"]["hybrid (full)"]["location"]
    assert loc["tp"] == 1 and loc["fn"] == 0


def test_injected_by_category_rows():
    cases, report = fixture()
    rows = scoring.score_injected_by_category(cases, report, CONFIG, verify=False)
    entry = rows["test"]["hybrid (full)"]["bug-hunter"]
    assert entry["cases"] == 2 and entry["location"]["tp"] >= 1
    text = scoring.category_table(rows["test"])
    assert "bug-hunter" in text and "hybrid (full)" in text


def test_tables_render_markdown():
    cases, report = fixture()
    rows = scoring.score_report(cases, report, CONFIG, verify=False)["test"]
    for table in (
        scoring.policy_table(rows, "location"),
        scoring.policy_table(rows, "category"),
        scoring.extras_table(rows),
        scoring.health_table(rows),
    ):
        assert table.startswith("|") and "hybrid (full)" in table
    assert "0.667" in scoring.policy_table(rows, "location")
    assert scoring._fmt(None) == "n/a"


def test_rule_statistics_prune_candidates_and_tuning_use_dev_only():
    cases, report = fixture()
    static = {}
    for case in cases:
        static[case.case_id] = CaseRecord(
            findings=[
                finding(line=100 + i, source="ruff", rule_id="S608", confidence=1.0)
                for i in range(3)
            ]
            + [
                finding(line=10 + i % 2, source="ruff", rule_id="B006", confidence=1.0)
                for i in range(3)
            ]
        )
    report.predictions["static-only"] = static
    dev = [c for c in cases if c.metadata["split"] == "dev"]
    stats = scoring.rule_statistics(dev, report, "static-only")
    assert stats["S608"] == {"fired": 3, "hit": 0} and stats["B006"] == {"fired": 3, "hit": 3}
    assert scoring.prune_candidates(stats, FocusConfig(), min_fired=3) == ["S608"]
    assert scoring.prune_candidates(stats, FocusConfig(), min_fired=5) == []
    tuned = scoring.tune([("human", cases, report)])
    assert tuned["tuned_on"] == "dev split only" and tuned["grid_size"] > 100
    assert tuned["best"]["mean_f1"] >= tuned["top10"][-1]["mean_f1"]
    assert tuned["benchmarks"] == ["human"]


def test_adjudication_sample_is_seeded_and_csv_has_empty_review_columns(tmp_path):
    cases, report = fixture()
    rows = scoring.adjudication_rows(cases, report, CONFIG, verify=False, size=1, seed=3)
    again = scoring.adjudication_rows(cases, report, CONFIG, verify=False, size=1, seed=3)
    assert rows == again and rows[-1]["_pool"] == 1  # focused mode already hides line 50
    all_rows = scoring.adjudication_rows(cases, report, CONFIG, verify=False, size=50)
    output = tmp_path / "adj.csv"
    assert scoring.write_adjudication(all_rows, output) == 1
    parsed = list(csv.DictReader(output.open(encoding="utf-8")))
    assert {r["team_valid"] for r in parsed} == {""} and {r["notes"] for r in parsed} == {""}
    assert {int(r["line"]) for r in parsed} == {30}


def test_adjudication_sampling_depends_on_the_seed_only():
    cases, report = fixture()
    many = [finding(line=100 + 3 * i, confidence=0.95) for i in range(12)]
    report.predictions["hybrid-multi-agent"]["c2"].findings.extend(many)
    pick = lambda seed: [  # noqa: E731
        r["line"]
        for r in scoring.adjudication_rows(cases, report, CONFIG, verify=False, size=4, seed=seed)
        if "line" in r
    ]
    assert pick(1) == pick(1) and len(pick(1)) == 4
    assert pick(1) != pick(2)


def test_score_and_tune_command_line(tmp_path, monkeypatch):
    cases, report = fixture()
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("".join(c.model_dump_json() + "\n" for c in cases), encoding="utf-8")
    results = tmp_path / "results.json"
    results.write_text(report.model_dump_json(), encoding="utf-8")
    out, tuning, adjudication = tmp_path / "s.json", tmp_path / "t.json", tmp_path / "a.csv"
    args = ["--human", str(corpus), str(results), "--injected", str(corpus), str(results)]
    monkeypatch.setattr(sys, "argv", ["score", "tune", *args, "--output", str(tuning)])
    scoring.main()
    assert json.loads(tuning.read_text())["tuned_on"] == "dev split only"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "score",
            "score",
            *args,
            "--output",
            str(out),
            "--adjudication",
            str(adjudication),
            "--no-verify",
        ],
    )
    scoring.main()
    payload = json.loads(out.read_text())
    assert set(payload["benchmarks"]) == {"human-comments", "injected-defects"}
    human = payload["benchmarks"]["human-comments"]
    assert human["ground_truth_counts"]["test"]["actionable"] == 2
    assert human["adjudication_rows"] == 1 and adjudication.exists()
    assert "by_category" in payload["benchmarks"]["injected-defects"]


# ---- labelling ----------------------------------------------------------------------------

DIFF = injection.new_file_diff("m.py", "a = 1\nb = None\nc = b.strip()\n")


def labelled_pair():
    def build(case_id, texts):
        return EvaluationCase(
            case_id=case_id,
            diff=DIFF,
            human_findings=[finding(line=3, category="style") for _ in texts],
            metadata={
                "repo": "o/r",
                "split": "dev",
                "finding_labels": [
                    {
                        "comment_text": text,
                        "comment_url": f"https://x/{index}",
                        "heuristic_category": heur,
                        "addressed": addressed,
                        "outdated": addressed,
                    }
                    for index, (text, heur, addressed) in enumerate(texts)
                ],
            },
        )

    return [
        build("a", [("crashes on None", "bug-hunter", True), ("thanks", "style", False)]),
        build("b", [("rename this", "style", False)]),
    ]


CALLS = {"n": 0}


def judged(items):
    body = {
        "judgements": [
            {"id": i, "actionable": a, "category": c, "reason": "r"}
            for i, (a, c) in enumerate(items)
        ]
    }

    def handle(request):
        CALLS["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})

    return handle


def test_label_corpus_skips_labelled_and_reports_failures():
    cases = labelled_pair()
    client = OpenAICompatibleClient(
        api_key="k",
        model="mm",
        transport=httpx.MockTransport(judged([(True, "bug-hunter"), (False, "style")])),
        max_retries=0,
    )
    seen = []
    failed = asyncio.run(labeling.label_corpus(cases, client, on_progress=seen.append))
    # case "b" has one comment but the mock answers with two ids: it fails, "a" is labelled.
    assert failed == ["b"] and len(seen) == 2
    assert cases[0].metadata["labelled"] and not labeling.is_labelled(cases[1])
    before = CALLS["n"]
    again = asyncio.run(labeling.label_corpus(cases, client))
    assert again == ["b"] and CALLS["n"] - before == 2  # only case b (one call + one retry)


def test_label_statistics_and_actionable_filtering_hand_computed():
    cases = labelled_pair()
    labeling.apply_judgements(
        cases[0],
        {
            0: labeling.Judgement(id=0, actionable=True, category="bug-hunter", reason="r"),
            1: labeling.Judgement(id=1, actionable=False, category="style", reason="r"),
        },
        "mm",
    )
    labeling.apply_judgements(
        cases[1],
        {0: labeling.Judgement(id=0, actionable=True, category="architecture", reason="r")},
        "mm",
    )
    stats = labeling.label_statistics(cases)
    assert stats["comments"] == 3 and stats["actionable"] == 2
    assert stats["actionable_rate"] == pytest.approx(2 / 3)
    assert stats["category_agreement"] == pytest.approx(2 / 3)  # 2 of 3 categories agree
    assert stats["category_agreement_actionable"] == pytest.approx(0.5)
    assert stats["addressed"] == 1 and stats["actionable_rate_addressed"] == 1.0
    assert stats["classifier_categories_actionable"] == {"bug-hunter": 1, "architecture": 1}
    assert [f.line for f in ground_truth(cases[0])] == [3]  # "thanks" is filtered out
    assert len(ground_truth(cases[0], "all")) == 2
    section = labeling.labelling_section(stats, "mm", "2026-01-01 00:00")
    assert "66.7%" in section and "Cohen" in section
    context = labeling.code_context(cases[0], "m.py", 3)
    assert (
        context.splitlines()[-1].startswith("> 3:")
        and labeling.code_context(cases[0], "nope.py", 3) == ""
    )


def test_team_csv_has_empty_team_columns(tmp_path):
    cases = labelled_pair()
    labeling.apply_judgements(
        cases[1],
        {0: labeling.Judgement(id=0, actionable=True, category="style", reason="why")},
        "mm",
    )
    output = tmp_path / "labels.csv"
    assert labeling.write_team_csv(cases, output) == 3
    rows = list(csv.DictReader(output.open(encoding="utf-8")))
    assert list(rows[0]) == list(labeling.CSV_COLUMNS)
    assert {r["team_actionable"] for r in rows} == {""} and {r["team_category"] for r in rows} == {
        ""
    }
    assert (
        rows[0]["addressed_evidence"] == "outdated" and rows[2]["classifier_actionable"] == "True"
    )


def test_labeling_main_writes_corpus_stats_card_and_csv(tmp_path, monkeypatch):
    cases = labelled_pair()[1:]
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(cases[0].model_dump_json() + "\n", encoding="utf-8")
    transport = httpx.MockTransport(judged([(True, "style")]))
    real = OpenAICompatibleClient
    monkeypatch.setattr(
        labeling,
        "OpenAICompatibleClient",
        lambda **kw: real(**{**kw, "transport": transport, "max_retries": 0}),
    )
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "mm")
    argv = ["label", str(corpus), "--delay", "0"]
    for flag, name in (("--csv", "l.csv"), ("--card", "c.md"), ("--stats", "s.json")):
        argv += [flag, str(tmp_path / name)]
    monkeypatch.setattr(sys, "argv", argv)
    labeling.main()
    assert json.loads((tmp_path / "s.json").read_text())["actionable"] == 1
    assert "Labelling (LLM classifier" in (tmp_path / "c.md").read_text()
    assert load_jsonl(corpus)[0].metadata["labelled"] is True
    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(SystemExit):
        labeling.main()


# ---- injection ----------------------------------------------------------------------------

SAMPLE = '''import os


def load(path, mode=None, retries=3):
    """Load a file."""
    handle = open(path)
    try:
        data = handle.read()
    except OSError:
        raise
    for index in range(len(data)):
        if index == 3:
            break
    return data


def other(items, flag=None):
    return [item for item in items if item]
'''


@pytest.mark.parametrize(
    "mutator", [m for group in injection.MUTATORS.values() for m in group], ids=lambda m: m.__name__
)
def test_every_defect_generator_records_an_added_line_and_valid_python(mutator):
    options = mutator(SAMPLE, ast.parse(SAMPLE), random.Random(1))  # noqa: S311
    assert options, f"{mutator.__name__} found no site in the sample"
    for option in options:
        compile(option.source, "x", "exec")
        diff = injection.new_file_diff("m.py", option.source)
        parsed = parse_diff(diff)[0]
        assert option.line in parsed.added_lines()
        assert option.source.split("\n")[option.line - 1].strip() == option.after
        assert option.category in injection.CATEGORIES and option.mutation


def test_injection_main_card_and_candidates(tmp_path, monkeypatch):
    root = tmp_path / "src_case"
    (root / "pkg").mkdir(parents=True)
    body = SAMPLE + "\n".join(f"# filler {i}" for i in range(40)) + "\n"
    for index in range(6):
        (root / "pkg" / f"m{index}.py").write_text(body, encoding="utf-8")
    (root / "pkg" / "test_skip.py").write_text(body, encoding="utf-8")
    (root / "pkg" / "broken.py").write_text("def (:\n" + "x\n" * 40, encoding="utf-8")
    source = EvaluationCase(
        case_id="s",
        diff="",
        repo_path=str(root),
        metadata={"repo": "o/r", "split": "test"},
    )
    names = {Path(rel).name for _, _, rel, _ in injection.candidate_files([source])}
    assert "test_skip.py" not in names and "broken.py" not in names and len(names) == 6
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(source.model_dump_json() + "\n", encoding="utf-8")
    out, card = tmp_path / "inj.jsonl", tmp_path / "INJ.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inject",
            "--corpus",
            str(corpus),
            "--output",
            str(out),
            "--root",
            str(tmp_path / "injected"),
            "--card",
            str(card),
            "--per-category",
            "2",
        ],
    )
    injection.main()
    cases = load_jsonl(out)
    assert len(cases) == 8 and "Injected-defect benchmark card" in card.read_text()
    assert {c.metadata["split"] for c in cases} == {"test"}


# ---- evaluation and corpus entry points ---------------------------------------------------


def test_verify_record_retries_then_records_the_error_and_verdicts():
    case = make_case("c", "dev", [finding(line=2)])
    cand = finding(line=2, title="claim")
    calls = {"n": 0}

    def handle(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": {"message": "x"}})
        body = {"verdicts": [{"id": 0, "grounded": False, "reason": "no"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})

    client = OpenAICompatibleClient(
        api_key="k", model="m", transport=httpx.MockTransport(handle), max_retries=0
    )
    record = CaseRecord(findings=[cand])
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    asyncio.run(verify_record(client, case, record, passes=3, wait=5, sleep=sleep))
    assert record.verified and record.verify_error is None and waits == [5]
    assert record.verdicts == {finding_key(cand): False}
    dead = OpenAICompatibleClient(
        api_key="k",
        model="m",
        transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})),
        max_retries=0,
    )
    again = CaseRecord(findings=[cand])
    asyncio.run(verify_record(dead, case, again, passes=2, wait=1, sleep=sleep))
    assert again.verified and again.verify_error


def test_evaluation_main_static_run_checkpoint_resume_and_environment(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    case = EvaluationCase(
        case_id="e1",
        diff=injection.new_file_diff("m.py", "a = 1\nb = 2\n"),
        human_findings=[finding(line=2)],
        repo_path=str(root),
        metadata={"repo": "o/r", "split": "dev"},
    )
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(case.model_dump_json() + "\n", encoding="utf-8")
    output, checkpoint = tmp_path / "r.json", tmp_path / "ck.json"
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["evaluate", str(corpus), "--output", str(output), "--checkpoint", str(checkpoint)],
    )
    evaluation_module.main()
    report = EvaluationReport.model_validate_json(output.read_text())
    assert [s.strategy for s in report.strategies] == ["static-only"]
    assert set(report.strategies_not_run) == {"single-agent", "hybrid-multi-agent"}
    assert report.environment["corpus_sha256"] and report.run_config["max_passes"] == 3
    assert "e1" in report.predictions["static-only"]
    assert "e1" in json.loads(checkpoint.read_text())["static-only"]
    evaluation_module.main()  # resumes from the checkpoint without re-running the case
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_jsonl(empty)


def test_corpus_card_main_and_later_changes(tmp_path, monkeypatch):
    cases = labelled_pair()
    labeling.apply_judgements(
        cases[0],
        {
            0: labeling.Judgement(id=0, actionable=True, category="bug-hunter", reason="r"),
            1: labeling.Judgement(id=1, actionable=False, category="style", reason="r"),
        },
        "mm",
    )
    for case in cases:
        case.metadata.update({"pr_number": 1, "split": case.metadata["split"]})
    card = corpus_module.corpus_card(cases, "abc", "2026-01-01 00:00")
    assert "Cases: 2; human-comment findings: 3" in card and "LLM-classified comments: 2" in card
    assert "| o/r | dev | 2 | 3 |" in card

    async def fake_build(repos, **kwargs):
        return cases

    monkeypatch.setattr(corpus_module, "build_corpus", fake_build)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    output = tmp_path / "corpus.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build",
            "o/r",
            "--output",
            str(output),
            "--card",
            str(tmp_path / "CORPUS.md"),
            "--repos-dir",
            str(tmp_path / "repos"),
        ],
    )
    corpus_module.main()
    assert len(load_jsonl(output)) == 2 and (tmp_path / "CORPUS.md").exists()
    monkeypatch.setattr(sys, "argv", ["build", "o/r", "--output", str(output), "--merge"])
    corpus_module.main()  # merge keeps the existing cases and adds none twice
    assert len(load_jsonl(output)) == 2

    later = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"

    def handle(request):
        if "compare/c1...h1" in request.url.path:
            return httpx.Response(200, text=later)
        return httpx.Response(404)

    async def run():
        fetcher = corpus_module.GitHubFetcher("t", transport=httpx.MockTransport(handle))
        try:
            same = await corpus_module._later_changes(fetcher, "o", "r", "h1", {"sha": "h1"})
            none = await corpus_module._later_changes(fetcher, "o", "r", "c1", {})
            diff = await corpus_module._later_changes(fetcher, "o", "r", "c1", {"sha": "h1"})
            missing = await corpus_module._later_changes(fetcher, "o", "r", "zz", {"sha": "h1"})
        finally:
            await fetcher.aclose()
        return same, none, diff, missing

    same, none, diff, missing = asyncio.run(run())
    assert same == {} and none is None and diff == {"m.py": {2}} and missing is None


# ---- cli ----------------------------------------------------------------------------------


def test_cli_offline_smoke_on_demo_diff(monkeypatch, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("REVIEW_MODE", "full")
    demo = ROOT / "examples" / "demo.diff"
    monkeypatch.setattr(sys, "argv", ["pr-reviewer", str(demo), "--offline"])
    cli_main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"] and isinstance(payload["findings"], list) and payload["summary"]
    assert any(f["source"] for f in payload["findings"])


def test_limit_per_repo_keeps_first_cases_of_each_repository_in_order():
    cases = [
        EvaluationCase(case_id=f"{repo}{i}", diff="", metadata={"repo": repo})
        for repo in ("a", "b")
        for i in range(3)
    ]
    kept = evaluation_module.limit_per_repo(cases, 2)
    assert [c.case_id for c in kept] == ["a0", "a1", "b0", "b1"]


def test_bootstrap_interval_is_exact_for_constant_cases_and_wide_for_mixed_ones():
    constant = [(1, 1, 0)] * 6  # every case: one hit, one false alarm
    ci = scoring.bootstrap_ci(constant)
    assert ci["n_cases"] == 6 and ci["p"] == [0.5, 0.5] and ci["r"] == [1.0, 1.0]
    mixed = [(1, 0, 0), (0, 1, 1)] * 3
    wide = scoring.bootstrap_ci(mixed)
    assert wide["p"][0] < 0.5 < wide["p"][1] and wide["draws"] == scoring.BOOTSTRAP_DRAWS
    assert scoring.bootstrap_ci(mixed) == wide  # fixed seed: reproducible
    assert scoring.bootstrap_ci([]) == {"n_cases": 0}


def test_paired_difference_flags_intervals_that_include_zero():
    better = [(1, 0, 0)] * 8
    worse = [(0, 1, 1)] * 8
    clear = scoring.paired_difference(better, worse)
    assert clear["f1"]["diff"] == 1.0 and clear["f1"]["includes_zero"] is False
    same = scoring.paired_difference(better, list(better))
    assert same["f1"]["diff"] == 0 and same["f1"]["includes_zero"] is True
    noisy_a, noisy_b = [(1, 0, 0), (0, 1, 1)] * 3, [(0, 1, 1), (1, 0, 0)] * 3
    assert scoring.paired_difference(noisy_a, noisy_b)["f1"]["includes_zero"] is True
    assert scoring.paired_difference([], [])["n_cases"] == 0


def test_score_report_carries_cis_pairs_and_skips_incomplete_cases():
    cases, report = fixture()
    cases.append(make_case("never-run", "test", [finding(line=3)]))
    tables = scoring.score_report(cases, report, CONFIG, verify=False)
    row = tables["test"]["hybrid (full)"]
    assert row["cases"] == 2 and row["ci"]["location"]["n_cases"] == 2
    pair = tables["test"]["_paired"]["hybrid (full) minus static-only"]["location"]
    assert pair["n_cases"] == 2 and "includes_zero" in pair["f1"]
    text = scoring.policy_table(tables["test"], "location")
    assert "[" in text and "never-run" not in text
    assert "includes 0" in scoring.paired_table(tables["test"])


def test_case_is_clean_requires_every_agent_and_verification_to_succeed():
    ok = CaseRecord(agent_runs=success())
    bad_run = CaseRecord(
        agent_runs=[AgentRun(agent="a", status="timed_out", duration_ms=1, finding_count=0)]
    )
    bad_verify = CaseRecord(agent_runs=success(), verify_error="x")
    preds = {"s": {"1": ok, "2": bad_run, "3": bad_verify}, "h": {"1": ok, "2": ok, "3": ok}}
    assert evaluation_module.case_is_clean(preds, ["s", "h"], "1")
    assert not evaluation_module.case_is_clean(preds, ["s", "h"], "2")
    assert not evaluation_module.case_is_clean(preds, ["s", "h"], "3")
