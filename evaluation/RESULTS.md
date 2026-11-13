# Evaluation results (partial: free-tier quota)

Generated 2026-11-14 by `scripts/render_results.py` from `evaluation/scores/*.json` (scoring commit
`410e4cf`). Every interval is a 95% percentile bootstrap over cases (2000 draws, seed
20261114); `n` is the number of cases, not comments. Plans are not outcomes: this file
says what ran.

## 0. Post-fix re-run of the human-comment benchmark (2026-11-14, partial: n = 3)

**What changed.** Commit `341b7ad` gives the review agents a line-numbered diff and snaps
near-miss findings to an added line. Before it, agents had to count lines from hunk headers and
findings on mis-numbered lines were dropped: **3 agent findings kept in
151 agent runs** over 31 cases (pre-fix checkpoint;
single agent 1 in 31 runs). Section 3 below is
the **pre-fix** run and is kept for the record only.

**What ran (2026-11-14, 04:02-04:32 IST).** Model probe: `gemini-3-flash-preview` and
`gemini-2.5-flash-lite` answered; `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` returned 429;
`gemini-flash-latest`, `gemini-3.7-flash` and `gemini-3.8-flash` did not answer within 40 s.
`gemini-3-flash-preview` was used for every LLM step (single agent, hybrid specialists, verifier),
test split only, the first 2 cases of each test repository, agents sequential and paced, up to 3
passes. Each call took about 70-80 s, and the model's free tier stopped at **20 requests per day**
(429 `GenerateRequestsPerDayPerProjectPerModel-FreeTier`) after the single agent and verifier had
finished **3 test cases**. **No hybrid case completed**, so there is no hybrid row and no
hybrid-minus-single or hybrid-minus-static difference for the post-fix code. A second attempt on
`gemini-2.5-flash-lite` and then `gemini-2.5-flash` hit the same daily cap before finishing one
case and is not used. Predictions from code commit `410e4cf2735dc62b0448a05287dc3ec6c2b67425`.

**Agent output after the fix:** the single agent kept **10 findings in 3
runs** (pre-fix: 1 in 31; all agents
3 in 151); findings still dropped as not on an added line:
0; failed agent runs: 0. This shows the fix lets agent findings
through; with n = 3 it does **not** support any claim about rates.

#### 0a. Test split, post-fix, static-only vs single agent on the same 3 cases (location policy primary)

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 3 | 0 | 18 | 22 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| single-agent | 3 | 3 | 25 | 19 | 0.107 [0.00, 0.75] | 0.136 [0.00, 0.75] | 0.120 [0.00, 0.75] |
| static-only (focused) | 3 | 0 | 0 | 22 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| single-agent (focused) | 3 | 3 | 5 | 19 | 0.375 [0.00, 0.75] | 0.136 [0.00, 0.75] | 0.200 [0.00, 0.75] |

Paired differences (location-only, same cases, bootstrap 95% CI):

| Comparison (A minus B) | n cases | dP (95% CI) | dR (95% CI) | dF1 (95% CI) | F1 interval includes 0? |
| --- | ---: | ---: | ---: | ---: | --- |
| single-agent minus static-only | 3 | +0.107 [+0.00, +0.75] | +0.136 [+0.00, +0.75] | +0.120 [+0.00, +0.75] | yes: not distinguishable from 0 |
| single-agent (focused) minus static-only (focused) | 3 | +0.375 [+0.00, +0.75] | +0.136 [+0.00, +0.75] | +0.200 [+0.00, +0.75] | yes: not distinguishable from 0 |

Category-matching policy (secondary):

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 3 | 0 | 18 | 22 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| single-agent | 3 | 0 | 28 | 22 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| static-only (focused) | 3 | 0 | 0 | 22 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| single-agent (focused) | 3 | 0 | 8 | 22 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |

Hunk-level match and precision@k (location window):

| Strategy | Hunk P (95% CI) | Hunk R (95% CI) | Hunk F1 (95% CI) | P@1 | P@3 | P@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 0.500 [0.00, 0.60] | 0.409 [0.00, 0.90] | 0.450 [0.00, 0.72] | 0.000 | 0.000 | 0.000 |
| single-agent | 0.464 [0.00, 1.00] | 0.591 [0.00, 1.00] | 0.520 [0.00, 1.00] | 0.000 | 0.222 | 0.214 |
| static-only (focused) | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 | 0.000 | 0.000 |
| single-agent (focused) | 0.625 [0.00, 1.00] | 0.227 [0.00, 1.00] | 0.333 [0.00, 1.00] | 0.000 | 0.286 | 0.375 |

#### 0b. Agent health (post-fix run)

| Strategy | Cases | Agent runs | Failed on first pass | Failed after up to 3 passes | Mean latency per case (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| static-only | 3 | 0 | 0 | 0 | 1676 |
| single-agent | 3 | 3 | 0 | 0 | 74713 |

## 1. Status in plain words

- **This is a partial evaluation.** All LLM calls used free-tier Gemini/Gemma keys with hard
  per-model daily request caps (observed: 500 per day for `gemini-3.1-flash-lite`,
  `gemini-3.5-flash-lite` and `gemini-flash-lite-latest`; 20 per day for `gemini-2.5-flash-lite`
  and `gemini-3.6-flash`; `gemma-4-31b-it` returned 429 after two cases). Every model was
  exhausted on 2026-11-14 (reset about 13:30 IST). Runs stopped on their circuit breaker
  (two consecutive all-agents-failed cases) instead of burning more quota.
- **Human-comment benchmark** (144 cases, 534 comments, 15 repositories): static-only ran on all
  144. The LLM strategies (single-agent, hybrid, verifier) ran on **30
  cases** (21 test, 9 dev; model
  `gemini-3.5-flash-lite`, one model for every strategy), i.e. 30 of
  144 cases. One further case was dropped because all four hybrid agents failed on quota (429).
- **Injected-defect benchmark** (60 cases, 15 per category): static-only and single-agent ran on
  all 60; hybrid ran on 24 (the first 24 in corpus order: bug-hunter and performance only) and
  21 of those were clean (2 had failed agents, 1 a failed verification). Model
  `gemini-flash-lite-latest` for every strategy. Hybrid numbers therefore cover **two
  categories only** and are not comparable to the 60-case static/single numbers.
- Within a benchmark every strategy used the same model; the two benchmarks used different models
  (`gemini-3.5-flash-lite` vs `gemini-flash-lite-latest`) because quotas ran out at different
  times. Do not compare absolute numbers across benchmarks.
- Categories are LLM-labelled (`gemini-flash-lite-latest` for 465 comments,
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
LLM_MODEL=gemini-3.5-flash-lite python -m pr_reviewer.evaluation evaluation/corpus.jsonl \
  --output evaluation/results_human_extended.json --per-repo 6 --splits-first test,dev \
  --checkpoint evaluation/checkpoints/human_gemini-3.5-flash-lite.json \
  --sequential-agents --agent-delay 3 --case-delay 2 --agent-timeout 120 \
  --max-passes 3 --retry-wait 30 --redo-failed
LLM_MODEL=gemini-flash-lite-latest python -m pr_reviewer.evaluation \
  evaluation/injection_corpus.jsonl --output evaluation/results_injected_extended.json \
  --splits-first test,dev --checkpoint evaluation/checkpoints/injected_gemini-flash-lite-latest.json \
  --sequential-agents --agent-delay 3 --case-delay 2 --agent-timeout 120 \
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
  *actionable* or not and gave a category. Actionable: 413 of
  534 (77.3%). Agreement with the keyword heuristic on
  category: 58.2% (Cohen's kappa 0.14), so
  categories are weak. Ground truth for scoring = actionable comments
  (test: 175 of 213 comments).
- **Addressed** (the PR later dealt with the comment: GitHub marked it outdated, or a later
  commit rewrote exactly that line): 403 of 534 comments;
  83.6% of those are classified actionable versus
  58.0% of the rest. Outdated can also mean a force-push, so
  this is evidence, not proof.
- **Matching** (greedy one-to-one): *location-only* (primary) = same file within two lines; *hunk*
  = same diff hunk; *category* (secondary) = location plus the LLM category. **P@k** = share of the
  top-k findings of each PR that hit a comment.
- **Focused review mode** (`REVIEW_MODE=focused`, default): Semgrep rules plus Ruff S/B/F/E9
  (cosmetic members excluded), agent findings with confidence >= 0.7, at most 5 per file, optional
  verification pass (one LLM call per PR that drops findings it cannot ground in the diff),
  corroborated findings rank higher. **Dev-only tuning produced no signal**: with 9 dev cases
  every setting in the grid (192 combinations) scored location F1 = 0.0 for the
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

#### 3a. Test split, strategies on the same cases

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 21 | 11 | 188 | 74 | 0.055 [0.02, 0.10] | 0.129 [0.04, 0.22] | 0.077 [0.03, 0.12] |
| single-agent | 21 | 12 | 188 | 73 | 0.060 [0.03, 0.12] | 0.141 [0.05, 0.23] | 0.084 [0.04, 0.13] |
| hybrid (full) | 21 | 12 | 188 | 73 | 0.060 [0.03, 0.12] | 0.141 [0.05, 0.23] | 0.084 [0.04, 0.13] |
| static-only (focused) | 21 | 0 | 1 | 85 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| single-agent (focused) | 21 | 1 | 1 | 84 | 0.500 [0.00, 1.00] | 0.012 [0.00, 0.04] | 0.023 [0.00, 0.07] |
| hybrid (focused) | 21 | 1 | 1 | 84 | 0.500 [0.00, 1.00] | 0.012 [0.00, 0.04] | 0.023 [0.00, 0.07] |

Paired differences (location-only, same cases, bootstrap 95% CI):

| Comparison (A minus B) | n cases | dP (95% CI) | dR (95% CI) | dF1 (95% CI) | F1 interval includes 0? |
| --- | ---: | ---: | ---: | ---: | --- |
| single-agent minus static-only | 21 | +0.005 [+0.00, +0.03] | +0.012 [+0.00, +0.04] | +0.007 [+0.00, +0.03] | yes: not distinguishable from 0 |
| single-agent (focused) minus static-only (focused) | 21 | +0.500 [+0.00, +1.00] | +0.012 [+0.00, +0.04] | +0.023 [+0.00, +0.07] | yes: not distinguishable from 0 |
| hybrid (full) minus single-agent | 21 | +0.000 [+0.00, +0.00] | +0.000 [+0.00, +0.00] | +0.000 [+0.00, +0.00] | yes: not distinguishable from 0 |
| hybrid (full) minus static-only | 21 | +0.005 [+0.00, +0.03] | +0.012 [+0.00, +0.04] | +0.007 [+0.00, +0.03] | yes: not distinguishable from 0 |
| hybrid (focused) minus single-agent (focused) | 21 | +0.000 [+0.00, +0.00] | +0.000 [+0.00, +0.00] | +0.000 [+0.00, +0.00] | yes: not distinguishable from 0 |
| hybrid (focused) minus static-only (focused) | 21 | +0.500 [+0.00, +1.00] | +0.012 [+0.00, +0.04] | +0.023 [+0.00, +0.07] | yes: not distinguishable from 0 |
| hybrid (focused) minus hybrid (full) | 21 | +0.440 [-0.08, +0.95] | -0.129 [-0.22, -0.04] | -0.061 [-0.11, -0.01] | no |

Category-matching policy (secondary):

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 21 | 5 | 194 | 80 | 0.025 [0.00, 0.04] | 0.059 [0.00, 0.11] | 0.035 [0.00, 0.06] |
| single-agent | 21 | 5 | 195 | 80 | 0.025 [0.00, 0.04] | 0.059 [0.00, 0.11] | 0.035 [0.00, 0.06] |
| hybrid (full) | 21 | 6 | 194 | 79 | 0.030 [0.01, 0.06] | 0.071 [0.01, 0.12] | 0.042 [0.01, 0.07] |
| static-only (focused) | 21 | 0 | 1 | 85 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| single-agent (focused) | 21 | 0 | 2 | 85 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| hybrid (focused) | 21 | 1 | 1 | 84 | 0.500 [0.00, 1.00] | 0.012 [0.00, 0.04] | 0.023 [0.00, 0.07] |

Hunk-level match and precision@k (location window):

| Strategy | Hunk P (95% CI) | Hunk R (95% CI) | Hunk F1 (95% CI) | P@1 | P@3 | P@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 0.181 [0.07, 0.35] | 0.424 [0.14, 0.65] | 0.254 [0.10, 0.39] | 0.143 | 0.100 | 0.071 |
| single-agent | 0.185 [0.08, 0.35] | 0.435 [0.16, 0.66] | 0.260 [0.11, 0.39] | 0.200 | 0.129 | 0.093 |
| hybrid (full) | 0.185 [0.08, 0.35] | 0.435 [0.16, 0.66] | 0.260 [0.11, 0.39] | 0.200 | 0.129 | 0.093 |
| static-only (focused) | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 | 0.000 | 0.000 |
| single-agent (focused) | 0.500 [0.00, 1.00] | 0.012 [0.00, 0.04] | 0.023 [0.00, 0.07] | 0.500 | 0.500 | 0.500 |
| hybrid (focused) | 0.500 [0.00, 1.00] | 0.012 [0.00, 0.04] | 0.023 [0.00, 0.07] | 0.500 | 0.500 | 0.500 |

#### 3b. Dev + test, same cases (not tuned on; reported for size)

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 30 | 16 | 217 | 98 | 0.069 [0.04, 0.13] | 0.140 [0.07, 0.21] | 0.092 [0.05, 0.14] |
| single-agent | 30 | 17 | 217 | 97 | 0.073 [0.04, 0.14] | 0.149 [0.08, 0.22] | 0.098 [0.06, 0.15] |
| hybrid (full) | 30 | 17 | 217 | 97 | 0.073 [0.04, 0.14] | 0.149 [0.08, 0.22] | 0.098 [0.06, 0.15] |
| static-only (focused) | 30 | 0 | 1 | 114 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| single-agent (focused) | 30 | 1 | 1 | 113 | 0.500 [0.00, 1.00] | 0.009 [0.00, 0.03] | 0.017 [0.00, 0.06] |
| hybrid (focused) | 30 | 1 | 1 | 113 | 0.500 [0.00, 1.00] | 0.009 [0.00, 0.03] | 0.017 [0.00, 0.06] |

Paired differences (location-only, same cases, bootstrap 95% CI):

| Comparison (A minus B) | n cases | dP (95% CI) | dR (95% CI) | dF1 (95% CI) | F1 interval includes 0? |
| --- | ---: | ---: | ---: | ---: | --- |
| single-agent minus static-only | 30 | +0.004 [+0.00, +0.02] | +0.009 [+0.00, +0.03] | +0.005 [+0.00, +0.02] | yes: not distinguishable from 0 |
| single-agent (focused) minus static-only (focused) | 30 | +0.500 [+0.00, +1.00] | +0.009 [+0.00, +0.03] | +0.017 [+0.00, +0.06] | yes: not distinguishable from 0 |
| hybrid (full) minus single-agent | 30 | +0.000 [+0.00, +0.00] | +0.000 [+0.00, +0.00] | +0.000 [+0.00, +0.00] | yes: not distinguishable from 0 |
| hybrid (full) minus static-only | 30 | +0.004 [+0.00, +0.02] | +0.009 [+0.00, +0.03] | +0.005 [+0.00, +0.02] | yes: not distinguishable from 0 |
| hybrid (focused) minus single-agent (focused) | 30 | +0.000 [+0.00, +0.00] | +0.000 [+0.00, +0.00] | +0.000 [+0.00, +0.00] | yes: not distinguishable from 0 |
| hybrid (focused) minus static-only (focused) | 30 | +0.500 [+0.00, +1.00] | +0.009 [+0.00, +0.03] | +0.017 [+0.00, +0.06] | yes: not distinguishable from 0 |
| hybrid (focused) minus hybrid (full) | 30 | +0.427 [-0.11, +0.94] | -0.140 [-0.21, -0.07] | -0.080 [-0.14, -0.03] | no |

**Reading it.** Agents contributed one finding over 30 cases on this
model, so hybrid, single-agent and static-only are nearly identical and every paired interval
includes zero (all differences are 0 to 1 hit). The honest conclusion is *no measured difference*,
not "agents do not help": the near-empty agent output was later traced to the anchoring filter
dropping findings on mis-numbered lines (agents got a diff without line numbers; fixed in commit
`341b7ad`, re-run in section 0). Focused mode removes almost everything
(it keeps Semgrep and defect-family Ruff rules; most human comments are about naming, docs and
design), so its recall collapses: it is a precision mode, not a recall mode.

#### 3c. Addressed comments only (test split)

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 21 | 4 | 195 | 55 | 0.020 [0.00, 0.08] | 0.068 [0.01, 0.15] | 0.031 [0.01, 0.09] |
| single-agent | 21 | 5 | 195 | 54 | 0.025 [0.01, 0.09] | 0.085 [0.02, 0.17] | 0.039 [0.01, 0.10] |
| hybrid (full) | 21 | 5 | 195 | 54 | 0.025 [0.01, 0.09] | 0.085 [0.02, 0.17] | 0.039 [0.01, 0.10] |
| static-only (focused) | 21 | 0 | 1 | 59 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] |
| single-agent (focused) | 21 | 1 | 1 | 58 | 0.500 [0.00, 1.00] | 0.017 [0.00, 0.05] | 0.033 [0.00, 0.10] |
| hybrid (focused) | 21 | 1 | 1 | 58 | 0.500 [0.00, 1.00] | 0.017 [0.00, 0.05] | 0.033 [0.00, 0.10] |

#### 3d. Static-only on the whole corpus (model-free), test split and all 144 cases

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 68 | 22 | 297 | 153 | 0.069 [0.05, 0.11] | 0.126 [0.07, 0.19] | 0.089 [0.06, 0.13] |
| static-only (focused) | 68 | 1 | 12 | 174 | 0.077 [0.00, 0.17] | 0.006 [0.00, 0.02] | 0.011 [0.00, 0.03] |

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 144 | 56 | 630 | 357 | 0.082 [0.05, 0.13] | 0.136 [0.08, 0.20] | 0.102 [0.06, 0.15] |
| static-only (focused) | 144 | 3 | 73 | 410 | 0.039 [0.00, 0.08] | 0.007 [0.00, 0.02] | 0.012 [0.00, 0.03] |

#### 3e. Agent health (human benchmark, 30 clean cases)

| Strategy | Cases | Agent runs | Failed on first pass | Failed after up to 3 passes | Mean latency per case (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| static-only | 30 | 0 | 0 | 0 | 1320 |
| single-agent | 30 | 30 | 0 | 0 | 5268 |
| hybrid-multi-agent | 30 | 120 | 4 | 0 | 28789 |

## 4. Injected-defect benchmark

Model: `gemini-flash-lite-latest` for every strategy.

#### 4a. All 60 cases, static-only vs single-agent, test split (27 cases)

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 27 | 18 | 169 | 9 | 0.096 [0.07, 0.13] | 0.667 [0.48, 0.85] | 0.168 [0.12, 0.23] |
| single-agent | 27 | 18 | 182 | 9 | 0.090 [0.06, 0.12] | 0.667 [0.48, 0.85] | 0.159 [0.11, 0.21] |
| static-only (focused) | 27 | 3 | 0 | 24 | 1.000 [0.00, 1.00] | 0.111 [0.00, 0.22] | 0.200 [0.00, 0.36] |
| single-agent (focused) | 27 | 10 | 7 | 17 | 0.588 [0.41, 0.78] | 0.370 [0.19, 0.56] | 0.455 [0.27, 0.61] |

Paired differences (location-only, same cases, bootstrap 95% CI):

| Comparison (A minus B) | n cases | dP (95% CI) | dR (95% CI) | dF1 (95% CI) | F1 interval includes 0? |
| --- | ---: | ---: | ---: | ---: | --- |
| single-agent minus static-only | 27 | -0.006 [-0.01, -0.00] | +0.000 [+0.00, +0.00] | -0.010 [-0.02, -0.01] | no |
| single-agent (focused) minus static-only (focused) | 27 | -0.412 [-0.59, +0.63] | +0.259 [+0.11, +0.44] | +0.255 [+0.05, +0.48] | no |

#### 4b. All 60 cases, static-only vs single-agent, all splits (60 cases)

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 60 | 40 | 353 | 20 | 0.102 [0.08, 0.13] | 0.667 [0.55, 0.78] | 0.177 [0.14, 0.22] |
| single-agent | 60 | 43 | 372 | 17 | 0.104 [0.08, 0.13] | 0.717 [0.60, 0.83] | 0.181 [0.14, 0.22] |
| static-only (focused) | 60 | 10 | 0 | 50 | 1.000 [1.00, 1.00] | 0.167 [0.08, 0.27] | 0.286 [0.15, 0.42] |
| single-agent (focused) | 60 | 27 | 13 | 33 | 0.675 [0.55, 0.80] | 0.450 [0.32, 0.58] | 0.540 [0.41, 0.65] |

Paired differences (location-only, same cases, bootstrap 95% CI):

| Comparison (A minus B) | n cases | dP (95% CI) | dR (95% CI) | dF1 (95% CI) | F1 interval includes 0? |
| --- | ---: | ---: | ---: | ---: | --- |
| single-agent minus static-only | 60 | +0.002 [-0.01, +0.01] | +0.050 [+0.00, +0.12] | +0.004 [-0.01, +0.02] | yes: not distinguishable from 0 |
| single-agent (focused) minus static-only (focused) | 60 | -0.325 [-0.45, -0.20] | +0.283 [+0.17, +0.40] | +0.254 [+0.12, +0.41] | no |

#### 4c. Paired subset where hybrid ran cleanly: test split only (10 cases; bug-hunter and performance only)

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 10 | 6 | 66 | 4 | 0.083 [0.04, 0.14] | 0.600 [0.30, 0.90] | 0.146 [0.07, 0.24] |
| single-agent | 10 | 6 | 72 | 4 | 0.077 [0.04, 0.12] | 0.600 [0.30, 0.90] | 0.136 [0.07, 0.22] |
| hybrid (full) | 10 | 6 | 84 | 4 | 0.067 [0.03, 0.11] | 0.600 [0.30, 0.90] | 0.120 [0.06, 0.19] |
| static-only (focused) | 10 | 3 | 0 | 7 | 1.000 [0.00, 1.00] | 0.300 [0.00, 0.60] | 0.462 [0.00, 0.75] |
| single-agent (focused) | 10 | 5 | 4 | 5 | 0.556 [0.33, 0.80] | 0.500 [0.20, 0.80] | 0.526 [0.27, 0.72] |
| hybrid (focused) | 10 | 5 | 16 | 5 | 0.238 [0.13, 0.36] | 0.500 [0.20, 0.80] | 0.323 [0.16, 0.45] |

Paired differences (location-only, same cases, bootstrap 95% CI):

| Comparison (A minus B) | n cases | dP (95% CI) | dR (95% CI) | dF1 (95% CI) | F1 interval includes 0? |
| --- | ---: | ---: | ---: | ---: | --- |
| single-agent minus static-only | 10 | -0.006 [-0.01, -0.00] | +0.000 [+0.00, +0.00] | -0.010 [-0.02, -0.00] | no |
| single-agent (focused) minus static-only (focused) | 10 | -0.444 [-0.64, +0.00] | +0.200 [+0.00, +0.50] | +0.065 [-0.20, +0.44] | yes: not distinguishable from 0 |
| hybrid (full) minus single-agent | 10 | -0.010 [-0.02, -0.00] | +0.000 [+0.00, +0.00] | -0.016 [-0.03, -0.00] | no |
| hybrid (full) minus static-only | 10 | -0.017 [-0.04, -0.00] | +0.000 [+0.00, +0.00] | -0.026 [-0.06, -0.01] | no |
| hybrid (focused) minus single-agent (focused) | 10 | -0.317 [-0.49, -0.19] | +0.000 [+0.00, +0.00] | -0.204 [-0.31, -0.08] | no |
| hybrid (focused) minus static-only (focused) | 10 | -0.762 [-0.87, +0.00] | +0.200 [+0.00, +0.50] | -0.139 [-0.40, +0.22] | yes: not distinguishable from 0 |
| hybrid (focused) minus hybrid (full) | 10 | +0.171 [+0.08, +0.29] | -0.100 [-0.30, +0.00] | +0.203 [+0.07, +0.31] | no |

This test-only paired subset has 10 cases (< 12), so the all-splits paired subset is also given
(no tuning was done on this benchmark, so using its dev cases for scoring is legitimate):

#### 4d. Paired subset where hybrid ran cleanly: all splits (21 cases; bug-hunter and performance only)

| Strategy | n cases | TP | FP | FN | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | 21 | 13 | 123 | 8 | 0.096 [0.06, 0.15] | 0.619 [0.43, 0.81] | 0.166 [0.10, 0.25] |
| single-agent | 21 | 15 | 131 | 6 | 0.103 [0.07, 0.15] | 0.714 [0.52, 0.90] | 0.180 [0.12, 0.26] |
| hybrid (full) | 21 | 16 | 154 | 5 | 0.094 [0.06, 0.13] | 0.762 [0.57, 0.90] | 0.168 [0.12, 0.23] |
| static-only (focused) | 21 | 9 | 0 | 12 | 1.000 [1.00, 1.00] | 0.429 [0.24, 0.62] | 0.600 [0.38, 0.76] |
| single-agent (focused) | 21 | 13 | 7 | 8 | 0.650 [0.52, 0.80] | 0.619 [0.43, 0.81] | 0.634 [0.48, 0.76] |
| hybrid (focused) | 21 | 13 | 31 | 8 | 0.295 [0.22, 0.39] | 0.619 [0.43, 0.81] | 0.400 [0.30, 0.50] |

Paired differences (location-only, same cases, bootstrap 95% CI):

| Comparison (A minus B) | n cases | dP (95% CI) | dR (95% CI) | dF1 (95% CI) | F1 interval includes 0? |
| --- | ---: | ---: | ---: | ---: | --- |
| single-agent minus static-only | 21 | +0.007 [-0.01, +0.03] | +0.095 [+0.00, +0.24] | +0.014 [-0.01, +0.05] | yes: not distinguishable from 0 |
| single-agent (focused) minus static-only (focused) | 21 | -0.350 [-0.48, -0.20] | +0.190 [+0.05, +0.38] | +0.034 [-0.13, +0.26] | yes: not distinguishable from 0 |
| hybrid (full) minus single-agent | 21 | -0.009 [-0.02, +0.01] | +0.048 [+0.00, +0.14] | -0.012 [-0.04, +0.02] | yes: not distinguishable from 0 |
| hybrid (full) minus static-only | 21 | -0.001 [-0.03, +0.02] | +0.143 [+0.00, +0.29] | +0.002 [-0.04, +0.04] | yes: not distinguishable from 0 |
| hybrid (focused) minus single-agent (focused) | 21 | -0.355 [-0.47, -0.27] | +0.000 [+0.00, +0.00] | -0.234 [-0.30, -0.16] | no |
| hybrid (focused) minus static-only (focused) | 21 | -0.705 [-0.78, -0.61] | +0.190 [+0.05, +0.38] | -0.200 [-0.36, +0.01] | yes: not distinguishable from 0 |
| hybrid (focused) minus hybrid (full) | 21 | +0.201 [+0.13, +0.29] | -0.143 [-0.29, +0.00] | +0.232 [+0.14, +0.32] | no |

#### 4e. Recall by injected category (location window), all 60 cases, all splits

| Strategy | Category | Cases | Detected (location) | Recall (95% CI) | Precision (95% CI) | F1 (95% CI) | Recall with category |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | architecture | 15 | 10 | 0.667 [0.40, 0.87] | 0.099 [0.08, 0.14] | 0.172 [0.13, 0.23] | 0.000 |
| static-only | bug-hunter | 15 | 11 | 0.733 [0.53, 0.93] | 0.100 [0.06, 0.17] | 0.176 [0.11, 0.29] | 0.667 |
| static-only | performance | 15 | 4 | 0.267 [0.07, 0.47] | 0.041 [0.01, 0.09] | 0.071 [0.02, 0.15] | 0.000 |
| static-only | style | 15 | 15 | 1.000 [1.00, 1.00] | 0.176 [0.12, 0.29] | 0.300 [0.22, 0.45] | 0.667 |
| single-agent | architecture | 15 | 10 | 0.667 [0.40, 0.87] | 0.096 [0.07, 0.13] | 0.168 [0.13, 0.22] | 0.000 |
| single-agent | bug-hunter | 15 | 12 | 0.800 [0.60, 1.00] | 0.103 [0.07, 0.16] | 0.182 [0.12, 0.27] | 0.467 |
| single-agent | performance | 15 | 6 | 0.400 [0.13, 0.67] | 0.058 [0.02, 0.12] | 0.101 [0.03, 0.21] | 0.000 |
| single-agent | style | 15 | 15 | 1.000 [1.00, 1.00] | 0.167 [0.12, 0.27] | 0.286 [0.21, 0.42] | 0.667 |
| static-only (focused) | architecture | 15 | 0 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 |
| static-only (focused) | bug-hunter | 15 | 9 | 0.600 [0.33, 0.80] | 1.000 [1.00, 1.00] | 0.750 [0.50, 0.89] | 0.600 |
| static-only (focused) | performance | 15 | 0 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 |
| static-only (focused) | style | 15 | 1 | 0.067 [0.00, 0.20] | 1.000 [0.00, 1.00] | 0.125 [0.00, 0.33] | 0.000 |
| single-agent (focused) | architecture | 15 | 2 | 0.133 [0.00, 0.33] | 0.500 [0.00, 1.00] | 0.211 [0.00, 0.48] | 0.000 |
| single-agent (focused) | bug-hunter | 15 | 11 | 0.733 [0.47, 0.93] | 0.611 [0.50, 0.78] | 0.667 [0.52, 0.79] | 0.400 |
| single-agent (focused) | performance | 15 | 4 | 0.267 [0.07, 0.53] | 0.571 [0.17, 1.00] | 0.364 [0.10, 0.64] | 0.000 |
| single-agent (focused) | style | 15 | 10 | 0.667 [0.40, 0.87] | 0.909 [0.76, 1.00] | 0.769 [0.57, 0.93] | 0.133 |

#### 4f. Recall by injected category, paired subset (hybrid), all splits

| Strategy | Category | Cases | Detected (location) | Recall (95% CI) | Precision (95% CI) | F1 (95% CI) | Recall with category |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| static-only | bug-hunter | 15 | 11 | 0.733 [0.53, 0.93] | 0.100 [0.06, 0.17] | 0.176 [0.11, 0.29] | 0.667 |
| static-only | performance | 6 | 2 | 0.333 [0.00, 0.67] | 0.077 [0.00, 0.20] | 0.125 [0.00, 0.29] | 0.000 |
| single-agent | bug-hunter | 15 | 12 | 0.800 [0.60, 1.00] | 0.103 [0.07, 0.16] | 0.182 [0.12, 0.27] | 0.467 |
| single-agent | performance | 6 | 3 | 0.500 [0.17, 0.83] | 0.103 [0.02, 0.27] | 0.171 [0.04, 0.40] | 0.000 |
| hybrid (full) | bug-hunter | 15 | 12 | 0.800 [0.60, 1.00] | 0.088 [0.06, 0.13] | 0.158 [0.11, 0.23] | 0.600 |
| hybrid (full) | performance | 6 | 4 | 0.667 [0.33, 1.00] | 0.121 [0.04, 0.26] | 0.205 [0.08, 0.41] | 0.167 |
| static-only (focused) | bug-hunter | 15 | 9 | 0.600 [0.33, 0.80] | 1.000 [1.00, 1.00] | 0.750 [0.50, 0.89] | 0.600 |
| static-only (focused) | performance | 6 | 0 | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 [0.00, 0.00] | 0.000 |
| single-agent (focused) | bug-hunter | 15 | 11 | 0.733 [0.47, 0.93] | 0.611 [0.50, 0.78] | 0.667 [0.52, 0.79] | 0.400 |
| single-agent (focused) | performance | 6 | 2 | 0.333 [0.00, 0.67] | 1.000 [0.00, 1.00] | 0.500 [0.00, 0.80] | 0.000 |
| hybrid (focused) | bug-hunter | 15 | 11 | 0.733 [0.47, 0.93] | 0.289 [0.21, 0.39] | 0.415 [0.30, 0.53] | 0.533 |
| hybrid (focused) | performance | 6 | 2 | 0.333 [0.00, 0.67] | 0.333 [0.00, 0.50] | 0.333 [0.00, 0.53] | 0.167 |

#### 4g. Agent health (injected benchmark, 21 clean paired cases)

| Strategy | Cases | Agent runs | Failed on first pass | Failed after up to 3 passes | Mean latency per case (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| static-only | 21 | 0 | 0 | 0 | 847 |
| single-agent | 21 | 21 | 0 | 0 | 4237 |
| hybrid-multi-agent | 21 | 84 | 1 | 0 | 34717 |

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
| Comments classified | 534 |
| Actionable | 413 (77.3%) |
| Category agreement (classifier vs keyword heuristic) | 58.2% |
| Cohen's kappa | 0.14 |
| Addressed comments | 403 |

## 7. Corpus and ground-truth sizes

Cases per split and comment counts: dev 76 cases /
238 actionable comments; test 68 cases /
175 actionable comments (addressed: dev 204,
test 133).

## 8. Adjudication

`evaluation/ADJUDICATION.csv` is the seeded random sample of hybrid-focused test-split
false positives on the human benchmark requested for the team: the pool contains
**1** finding(s) (focused mode leaves almost none), so it has
1 row(s) instead of 50. To still give the team something to judge,
`evaluation/ADJUDICATION_injected.csv` samples (seed 20261113) up to 50 hybrid-focused
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

## 10. Suggestion quality (offline, 2026-11-14)

Scored by `scripts/score_suggestions.py` (no LLM calls; output `evaluation/scores/suggestions.json`).
Unique validated suggestions (deduplicated by case, path, range and replacement across the four
result files and all strategies): 27. Each is applied to the head-version file.
Cells are k/n = rate [Wilson 95% CI]; n differs by column because a metric is scored only where it
applies (defect metrics need an injected case; the static-rule metric needs a ruff code that fired
near the finding). The injected benchmark's 24 and the human benchmark's 3 suggestions are all
from LLM findings; no validated suggestion came from a pure ruff/semgrep finding, and none had a
ruff-checkable rule that fired, so the static-resolution rate has n = 0 and is not reported.

| Group | n | applies | whole file parses | no collateral | hits defect | restores original | changes defect line | resolves static rule |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All | 27 | 27/27 = 1.00 [0.88, 1.00] | 20/27 = 0.74 [0.55, 0.87] | 18/27 = 0.67 [0.48, 0.81] | 13/24 = 0.54 [0.35, 0.72] | 1/24 = 0.04 [0.01, 0.20] | 13/24 = 0.54 [0.35, 0.72] | n/a (n=0) |
| Agent (LLM) | 27 | 27/27 = 1.00 [0.88, 1.00] | 20/27 = 0.74 [0.55, 0.87] | 18/27 = 0.67 [0.48, 0.81] | 13/24 = 0.54 [0.35, 0.72] | 1/24 = 0.04 [0.01, 0.20] | 13/24 = 0.54 [0.35, 0.72] | n/a (n=0) |
| Static (ruff/semgrep) | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Injected benchmark | 24 | 24/24 = 1.00 [0.86, 1.00] | 17/24 = 0.71 [0.51, 0.85] | 15/24 = 0.62 [0.43, 0.79] | 13/24 = 0.54 [0.35, 0.72] | 1/24 = 0.04 [0.01, 0.20] | 13/24 = 0.54 [0.35, 0.72] | n/a (n=0) |
| Human benchmark | 3 | 3/3 = 1.00 [0.44, 1.00] | 3/3 = 1.00 [0.44, 1.00] | 3/3 = 1.00 [0.44, 1.00] | n/a (n=0) | n/a (n=0) | n/a (n=0) | n/a (n=0) |

What this shows: the existing validator checks only the snippet, but 27/27
suggestions apply cleanly while only 20/27
leave the whole file parseable. All seven failures are block-structure breaks (five flush-left replacements inside an indented block, two
that delete or leave a block header orphaned), on files that parsed before, so a one-click "apply suggestion"
would break the file. About half of the injected-defect
suggestions touch the injected line (13/24) but only
1/24 restore the original code.
What it does not show: "parses", "touches the defect line" and "no new undefined names" are necessary
conditions, not proof of a correct fix, and restoring the exact original line is a strict bar that
rejects other valid fixes. No tests were run on patched code and no human judged the fixes. With
n = 27 (24 injected, 3 human, mostly from a handful of cases) the intervals are wide and the
suggestions within one case are not independent.
