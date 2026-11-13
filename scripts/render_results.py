# ruff: noqa: E501
"""Render evaluation/RESULTS.md from the saved score files (no LLM calls, no new numbers).

Usage: python scripts/render_results.py   (run from the repository root)
Every number in the tables is read from evaluation/scores/*.json, which pr_reviewer.scoring
produced from the saved predictions.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pr_reviewer import scoring as S

ROOT = Path("evaluation")


def load(name: str, benchmark: str) -> dict:
    return json.loads((ROOT / "scores" / f"{name}.json").read_text())["benchmarks"][benchmark]


def results(name: str) -> dict:
    return json.loads((ROOT / f"results_{name}.json").read_text())


def health(name: str) -> str:
    report = results(name)
    lines = [
        "| Strategy | Cases | Agent runs | Failed on first pass | Failed after up to 3 passes | "
        "Mean latency per case (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["strategies"]:
        lines.append(
            f"| {item['strategy']} | {item['cases']} | {item['agent_runs']} | "
            f"{item['initial_agent_failures']} | {item['agent_failures']} | "
            f"{item['mean_latency_ms']:.0f} |"
        )
    return "\n".join(lines)


def block(title: str, rows: dict, *, extras: bool = True, category_policy: bool = False) -> str:
    out = [f"#### {title}", "", S.policy_table(rows, "location"), ""]
    out += ["Paired differences (location-only, same cases, bootstrap 95% CI):", ""]
    out += [S.paired_table(rows), ""]
    if category_policy:
        out += ["Category-matching policy (secondary):", "", S.policy_table(rows, "category"), ""]
    if extras:
        out += ["Hunk-level match and precision@k (location window):", "", S.extras_table(rows), ""]
    return "\n".join(out)


POSTFIX = "human_test_gemini-3-flash-preview_postfix"
PREFIX_CHECKPOINT = ROOT / "checkpoints" / "human_gemini-3.5-flash-lite.json"
POSTFIX_CHECKPOINT = ROOT / "checkpoints" / f"{POSTFIX}.json"


def agent_output(checkpoint: Path, strategies=("single-agent", "hybrid-multi-agent")) -> dict:
    """Agent runs and agent findings kept (sum of finding_count over successful runs), per strategy."""
    import re

    data = json.loads(checkpoint.read_text())
    out: dict = {}
    for name in strategies:
        row = {"cases": 0, "runs": 0, "failed": 0, "kept": 0, "dropped": 0}
        for rec in data.get(name, {}).values():
            row["cases"] += 1
            for run in rec["agent_runs"]:
                row["runs"] += 1
                if run["status"] == "success":
                    row["kept"] += run["finding_count"] or 0
                else:
                    row["failed"] += 1
            for warning in rec.get("warnings", []):
                m = re.search(r"(\d+) finding\(s\) dropped", warning)
                row["dropped"] += int(m.group(1)) if m else 0
        out[name] = row
    out["total"] = {k: sum(out[n][k] for n in strategies) for k in out[strategies[0]]}
    return out


def postfix_section() -> str:
    """Section 0: the human-comment re-run after commit 341b7ad (numbered diff for the agents)."""
    if not (ROOT / "scores" / f"{POSTFIX}.json").exists():
        return ""
    pf = load(POSTFIX, "human-comments")
    rep = results(POSTFIX)
    t = pf["tables"]["test"]
    n = t["single-agent"]["cases"]
    pre = agent_output(PREFIX_CHECKPOINT)
    post = agent_output(POSTFIX_CHECKPOINT, ("single-agent",))["single-agent"]
    env = rep.get("environment", {})
    return f"""## 0. Post-fix re-run of the human-comment benchmark (2026-11-14, partial: n = {n})

**What changed.** Commit `341b7ad` gives the review agents a line-numbered diff and snaps
near-miss findings to an added line. Before it, agents had to count lines from hunk headers and
findings on mis-numbered lines were dropped: **{pre["total"]["kept"]} agent findings kept in
{pre["total"]["runs"]} agent runs** over {pre["single-agent"]["cases"]} cases (pre-fix checkpoint;
single agent {pre["single-agent"]["kept"]} in {pre["single-agent"]["runs"]} runs). Section 3 below is
the **pre-fix** run and is kept for the record only.

**What ran (2026-11-14, 04:02-04:32 IST).** Model probe: `gemini-3-flash-preview` and
`gemini-2.5-flash-lite` answered; `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` returned 429;
`gemini-flash-latest`, `gemini-3.7-flash` and `gemini-3.8-flash` did not answer within 40 s.
`gemini-3-flash-preview` was used for every LLM step (single agent, hybrid specialists, verifier),
test split only, the first 2 cases of each test repository, agents sequential and paced, up to 3
passes. Each call took about 70-80 s, and the model's free tier stopped at **20 requests per day**
(429 `GenerateRequestsPerDayPerProjectPerModel-FreeTier`) after the single agent and verifier had
finished **{n} test cases**. **No hybrid case completed**, so there is no hybrid row and no
hybrid-minus-single or hybrid-minus-static difference for the post-fix code. A second attempt on
`gemini-2.5-flash-lite` and then `gemini-2.5-flash` hit the same daily cap before finishing one
case and is not used. Predictions from code commit `{env.get("git_commit", "?")}`.

**Agent output after the fix:** the single agent kept **{post["kept"]} findings in {post["runs"]}
runs** (pre-fix: {pre["single-agent"]["kept"]} in {pre["single-agent"]["runs"]}; all agents
{pre["total"]["kept"]} in {pre["total"]["runs"]}); findings still dropped as not on an added line:
{post["dropped"]}; failed agent runs: {post["failed"]}. This shows the fix lets agent findings
through; with n = {n} it does **not** support any claim about rates.

{block(f"0a. Test split, post-fix, static-only vs single agent on the same {n} cases (location policy primary)", t, category_policy=True)}
#### 0b. Agent health (post-fix run)

{health(POSTFIX)}

"""


def main() -> None:
    commit = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    hp = load("human_paired", "human-comments")
    hs = load("human_static144", "human-comments")
    ip = load("injected_paired", "injected-defects")
    ia = load("injected_all60", "injected-defects")
    stats = json.loads((ROOT / "label_stats.json").read_text())
    tuning = json.loads((ROOT / "dev_tuning.json").read_text())
    counts = hp["ground_truth_counts"]
    parts: list[str] = []
    add = parts.append

    add(f"""# Evaluation results (partial: free-tier quota)

Generated 2026-11-14 by `scripts/render_results.py` from `evaluation/scores/*.json` (scoring commit
`{commit}`). Every interval is a 95% percentile bootstrap over cases (2000 draws, seed
{S.BOOTSTRAP_SEED}); `n` is the number of cases, not comments. Plans are not outcomes: this file
says what ran.

{postfix_section()}## 1. Status in plain words

- **This is a partial evaluation.** All LLM calls used free-tier Gemini/Gemma keys with hard
  per-model daily request caps (observed: 500 per day for `gemini-3.1-flash-lite`,
  `gemini-3.5-flash-lite` and `gemini-flash-lite-latest`; 20 per day for `gemini-2.5-flash-lite`
  and `gemini-3.6-flash`; `gemma-4-31b-it` returned 429 after two cases). Every model was
  exhausted on 2026-11-14 (reset about 13:30 IST). Runs stopped on their circuit breaker
  (two consecutive all-agents-failed cases) instead of burning more quota.
- **Human-comment benchmark** (144 cases, 534 comments, 15 repositories): static-only ran on all
  144. The LLM strategies (single-agent, hybrid, verifier) ran on **{hp["cases_completed"]["all"]}
  cases** ({hp["cases_completed"]["test"]} test, {hp["cases_completed"]["dev"]} dev; model
  `gemini-3.5-flash-lite`, one model for every strategy), i.e. {hp["cases_completed"]["all"]} of
  144 cases. One further case was dropped because all four hybrid agents failed on quota (429).
- **Injected-defect benchmark** (60 cases, 15 per category): static-only and single-agent ran on
  all 60; hybrid ran on 24 (the first 24 in corpus order: bug-hunter and performance only) and
  21 of those were clean (2 had failed agents, 1 a failed verification). Model
  `gemini-flash-lite-latest` for every strategy. Hybrid numbers therefore cover **two
  categories only** and are not comparable to the 60-case static/single numbers.
- Within a benchmark every strategy used the same model; the two benchmarks used different models
  (`gemini-3.5-flash-lite` vs `gemini-flash-lite-latest`) because quotas ran out at different
  times. Do not compare absolute numbers across benchmarks.
- Categories are LLM-labelled (`gemini-flash-lite-latest` for {stats["classified"] - 69} comments,
  `gemini-2.5-flash-lite` for 69) and are **pending the team's manual check**
  (`evaluation/LABELS-FOR-TEAM.csv`). Location-only matching is the primary policy because of this.
- Not measured: token usage and cost (checkpoint-based scoring does not carry the per-process
  counters), the false-positive validity (see section 8: the adjudication sample is mostly empty
  because focused mode leaves almost no human-benchmark false positives).

### Extending after the daily reset

The raw checkpoints are in `evaluation/checkpoints/`. To extend on the same code version, check
out the commit the predictions came from (`c4d2de3`), then for example:

```bash
git checkout c4d2de3
export LLM_API_KEY=$GEMINI_API_KEY LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_MODEL=gemini-3.5-flash-lite python -m pr_reviewer.evaluation evaluation/corpus.jsonl \\
  --output evaluation/results_human_extended.json --per-repo 6 --splits-first test,dev \\
  --checkpoint evaluation/checkpoints/human_gemini-3.5-flash-lite.json \\
  --sequential-agents --agent-delay 3 --case-delay 2 --agent-timeout 120 \\
  --max-passes 3 --retry-wait 30 --redo-failed
LLM_MODEL=gemini-flash-lite-latest python -m pr_reviewer.evaluation \\
  evaluation/injection_corpus.jsonl --output evaluation/results_injected_extended.json \\
  --splits-first test,dev --checkpoint evaluation/checkpoints/injected_gemini-flash-lite-latest.json \\
  --sequential-agents --agent-delay 3 --case-delay 2 --agent-timeout 120 \\
  --max-passes 3 --retry-wait 30 --redo-failed
```

Then re-score with `python -m pr_reviewer.scoring score ...` and `python scripts/render_results.py`.
Each run needs about 500 calls per model per day (7 calls per case: 1 single + 1 verify +
4 specialists + 1 verify). Resuming on a later commit changes the LLM request (strict JSON schema,
commit `3ba620b`), so extended cases must not be mixed into these checkpoints unless the same code
version is used; otherwise write a separate result file.

## 2. Method

- **Corpus** (`evaluation/CORPUS.md`): merged PRs of 15 public Python repositories, inline human
  review comments on added `.py` lines, at least 2 kept comments per case; 144 cases, 534
  comments. Built 2026-11-14 with the GitHub API; code is fetched as data and never executed.
- **Split by repository** (seeded hash, recorded in each case): dev = 7 repositories
  (76 cases), test = 8 repositories (68 cases). Nothing was tuned on test.
- **Labels**: an LLM classifier (temperature 0, strict JSON schema) judged each comment
  *actionable* or not and gave a category. Actionable: {stats["actionable"]} of
  {stats["classified"]} ({stats["actionable_rate"]:.1%}). Agreement with the keyword heuristic on
  category: {stats["category_agreement"]:.1%} (Cohen's kappa {stats["cohens_kappa"]:.2f}), so
  categories are weak. Ground truth for scoring = actionable comments
  (test: {counts["test"]["actionable"]} of {counts["test"]["all_comments"]} comments).
- **Addressed** (the PR later dealt with the comment: GitHub marked it outdated, or a later
  commit rewrote exactly that line): {stats["addressed"]} of {stats["comments"]} comments;
  {stats["actionable_rate_addressed"]:.1%} of those are classified actionable versus
  {stats["actionable_rate_not_addressed"]:.1%} of the rest. Outdated can also mean a force-push, so
  this is evidence, not proof.
- **Matching** (greedy one-to-one): *location-only* (primary) = same file within two lines; *hunk*
  = same diff hunk; *category* (secondary) = location plus the LLM category. **P@k** = share of the
  top-k findings of each PR that hit a comment.
- **Focused review mode** (`REVIEW_MODE=focused`, default): Semgrep rules plus Ruff S/B/F/E9
  (cosmetic members excluded), agent findings with confidence >= 0.7, at most 5 per file, optional
  verification pass (one LLM call per PR that drops findings it cannot ground in the diff),
  corroborated findings rank higher. **Dev-only tuning produced no signal**: with {hp["cases_completed"]["dev"]} dev cases
  every setting in the grid ({tuning["grid_size"]} combinations) scored location F1 = 0.0 for the
  hybrid, so the pre-registered principled defaults were kept unchanged
  (`evaluation/dev_tuning.json`). The verifier was run for every agent prediction here and is
  applied in the "(focused)" rows. Focused variants are computed offline from the same saved
  predictions.
- **Injected-defect benchmark** (`evaluation/INJECTION.md`): 60 diffs that each add one real
  corpus file with exactly one deterministic injected defect (seeded generator, recorded line):
  15 each of bug-hunter, performance, style, architecture. No tuning happened on it, so its dev
  cases are legitimately used for scoring; test = 27 cases, dev = 33. The surrounding code is real
  and unlabelled, so findings elsewhere count as false positives (precision is a lower bound).
- **Code version**: the agent predictions come from processes started 2026-11-14 01:31-01:35 IST
  (commit `c4d2de3` era; plain Gemini client). Static analysis is the 7125f24 layer (test-file
  exemptions, categories mapped to the four axes).

## 3. Human-comment benchmark (PRE-FIX: predictions from before commit 341b7ad; superseded by section 0)

Model for LLM strategies: `gemini-3.5-flash-lite` (every strategy). Agents ran sequentially
within a case with pacing; failed agents were re-run up to 3 passes.

{block("3a. Test split, strategies on the same cases", hp["tables"]["test"], category_policy=True)}
{block("3b. Dev + test, same cases (not tuned on; reported for size)", hp["tables"]["all"], extras=False)}
**Reading it.** Agents contributed one finding over {hp["cases_completed"]["all"]} cases on this
model, so hybrid, single-agent and static-only are nearly identical and every paired interval
includes zero (all differences are 0 to 1 hit). The honest conclusion is *no measured difference*,
not "agents do not help": the near-empty agent output was later traced to the anchoring filter
dropping findings on mis-numbered lines (agents got a diff without line numbers; fixed in commit
`341b7ad`, re-run in section 0). Focused mode removes almost everything
(it keeps Semgrep and defect-family Ruff rules; most human comments are about naming, docs and
design), so its recall collapses: it is a precision mode, not a recall mode.

#### 3c. Addressed comments only (test split)

{S.policy_table(hp["addressed_subset"]["test"], "location")}

#### 3d. Static-only on the whole corpus (model-free), test split and all 144 cases

{S.policy_table(hs["tables"]["test"], "location")}

{S.policy_table(hs["tables"]["all"], "location")}

#### 3e. Agent health (human benchmark, {hp["cases_completed"]["all"]} clean cases)

{health("human_paired")}

## 4. Injected-defect benchmark

Model: `gemini-flash-lite-latest` for every strategy.

{block("4a. All 60 cases, static-only vs single-agent, test split (27 cases)", ia["tables"]["test"], extras=False)}
{block("4b. All 60 cases, static-only vs single-agent, all splits (60 cases)", ia["tables"]["all"], extras=False)}
{block("4c. Paired subset where hybrid ran cleanly: test split only (10 cases; bug-hunter and performance only)", ip["tables"]["test"], extras=False)}
This test-only paired subset has 10 cases (< 12), so the all-splits paired subset is also given
(no tuning was done on this benchmark, so using its dev cases for scoring is legitimate):

{block("4d. Paired subset where hybrid ran cleanly: all splits (21 cases; bug-hunter and performance only)", ip["tables"]["all"], extras=False)}
#### 4e. Recall by injected category (location window), all 60 cases, all splits

{S.category_table(ia["by_category"]["all"])}

#### 4f. Recall by injected category, paired subset (hybrid), all splits

{S.category_table(ip["by_category"]["all"])}

#### 4g. Agent health (injected benchmark, 21 clean paired cases)

{health("injected_paired")}

**Reading it.** Without the verifier, the agents add a little recall over static-only on all
splits (single-agent +0.05 on 60 cases, hybrid +0.14 on the 21 paired cases; the recall intervals
touch zero and the F1 intervals include zero). On the test split alone they add no extra hits but
a few extra false positives, so the F1 differences are tiny and negative (-0.01 to -0.03) with
intervals that exclude zero. The
verifier plus focused mode raises precision from about 0.10 to about 0.65 for the single agent (at
the cost of recall), because the many static style findings on real code are hidden. The hybrid
focused configuration has a **lower** precision than single-agent focused on the same cases (its
four specialists produce more findings that pass the verifier): this is the opposite of the
project's hypothesis and the interval excludes zero. The hybrid covers only bug-hunter and
performance cases, so this is not the full picture.

## 5. What the data does and does not support

- Verified: on injected defects, the focused review mode (static rules + verifier + confidence
  cut) raises precision by roughly a factor of six over the unfiltered output for the single
  agent, with a recall cost; static analysis alone detects about two thirds of injected defects at
  location level but with about 10% precision.
- Not supported by these data: that the four-specialist hybrid beats a single agent or static
  analysis. All paired comparisons either include zero or favour the single agent on precision.
- Not measured: hybrid on style/architecture injections; any LLM result on more than 30 human
  cases; cost and tokens; whether "false positives" are really wrong (the real code was not
  audited).

## 6. Label statistics

| Quantity | Value |
| --- | ---: |
| Comments classified | {stats["classified"]} |
| Actionable | {stats["actionable"]} ({stats["actionable_rate"]:.1%}) |
| Category agreement (classifier vs keyword heuristic) | {stats["category_agreement"]:.1%} |
| Cohen's kappa | {stats["cohens_kappa"]:.2f} |
| Addressed comments | {stats["addressed"]} |

## 7. Corpus and ground-truth sizes

Cases per split and comment counts: dev {counts["dev"]["cases"]} cases /
{counts["dev"]["actionable"]} actionable comments; test {counts["test"]["cases"]} cases /
{counts["test"]["actionable"]} actionable comments (addressed: dev {counts["dev"]["addressed"]},
test {counts["test"]["addressed"]}).

## 8. Adjudication

`evaluation/ADJUDICATION.csv` is the seeded random sample of hybrid-focused test-split
false positives on the human benchmark requested for the team: the pool contains
**{hp.get("adjudication_pool", 0)}** finding(s) (focused mode leaves almost none), so it has
{hp.get("adjudication_rows", 0)} row(s) instead of 50. To still give the team something to judge,
`evaluation/ADJUDICATION_injected.csv` samples (seed {S.ADJUDICATION_SEED}) up to 50 hybrid-focused
false positives from the injected paired subset (all splits, 31 in the pool). Columns
`team_valid` and `notes` are empty for the team.

## 9. Limitations

- Small n: 21 human test cases and 10 injected paired test cases. Intervals are wide; several
  include zero and are labelled so. A bootstrap over cases ignores that comments within a PR
  correlate.
- Ground truth is weak: LLM-judged actionability and categories (unchecked by humans), and only
  comments a reviewer happened to write. A correct finding the reviewer did not mention counts as
  a false positive, so precision is a lower bound. Addressed-ness is partly proxied by outdated
  flags.
- Different models for the two benchmarks; one model per benchmark. A free-tier flash-lite model
  is not what a production deployment would run.
- The injected defects are obvious, synthetic, and inserted into whole-file diffs; real
  regressions are subtler. The hybrid covers two of four categories.
- The static layer changed during the project (7125f24: test-file exemptions); all static numbers
  here are from that layer.
- Pre-fix agent output on the human benchmark was almost empty (1 finding in 30 cases); the cause
  was later found (mis-numbered lines dropped; fixed in commit 341b7ad) and section 0 re-runs it.
- No human has yet checked the labels or the adjudication sample.
""")
    Path("evaluation/RESULTS.md").write_text("\n".join(parts), encoding="utf-8")
    print("wrote evaluation/RESULTS.md")


if __name__ == "__main__":
    main()
