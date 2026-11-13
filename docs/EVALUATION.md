# Reproducible evaluation

Use a held-out JSONL corpus. Each line must contain `case_id`, the exact unified `diff`, an optional isolated `repo_path`, and `human_findings` using the production `Finding` schema. The corpus must be separated from prompt or rule development. At least two reviewers should independently label each pull request and adjudicate disagreements before the file is frozen.

Run:

```bash
pr-reviewer-evaluate data/held_out.jsonl --output results/evaluation.json
```

With `LLM_API_KEY` configured, the runner compares static-only, one generalist agent, and the hybrid four-specialist pipeline. Without credentials it reports static-only rather than pretending that offline placeholders are agents. Two matching policies are reported for every strategy, both greedy one-to-one: `category` (same path, same category, within two lines; the flat counts and `per_category` precision/recall/F1 use it) and `location-only` (same path, within two lines, any category). Both exist because the corpus categories are heuristic, see below. The report contains confusion counts, precision, recall, F1, mean latency and each false positive for manual adjudication. An empty corpus is rejected. The report also records an `environment` block (model id or null, Ruff version, Semgrep availability and version, Python version, corpus SHA-256, git commit of the checkout). Without `LLM_API_KEY` the agent strategies appear under `strategies_not_run` with the reason, not as placeholder results.

## Building the corpus

```bash
GITHUB_TOKEN=$(gh auth token) pr-reviewer-build-corpus --max-prs 400 --max-cases-per-repo 7
```

`pr-reviewer-build-corpus` (`pr_reviewer/corpus.py`) reads merged PRs of public Python repositories (default: pallets/flask, psf/requests, encode/httpx, pydantic/pydantic, fastapi/fastapi, python-poetry/poetry), most recently updated first. `--max-prs` is the number of merged PRs scanned per repo. For each PR:

1. Fetch inline review comments and keep only top-level (no `in_reply_to_id`), human (`user.type == "User"`, not a `[bot]`), non-author comments on side `RIGHT` of a `.py` file.
2. Group by `original_commit_id`; try the commit with most comments (then the second), fetch the `base...commit` diff with the compare API (skipped above 200 KB or more than 25 changed `.py` files), reduce it to `.py` files and keep a comment only if its `(path, line)` is an added line in that diff. The line used is `original_line` (the line at the commit the comment was written on), falling back to `line`; this is a deliberate refinement because `line` is null once a comment is outdated.
3. Download the changed `.py` files at that commit through the contents API into `evaluation/repos/<case_id>/` as data. Repository code is never executed; paths are checked to stay inside the destination.
4. Each comment becomes a `Finding` (severity MEDIUM, message = first 500 characters, evidence = comment URL, source `human-review`, confidence 1.0). Case metadata stores repo, PR number and URL, commit SHA, base SHA and, per finding, `label_source = "heuristic-category"`.

Category heuristic (`CATEGORY_KEYWORDS` in `corpus.py`): each regex that matches counts once for its category; the highest count wins; ties go in the order bug-hunter, performance, architecture, style; no match defaults to style. Keyword lists:

- bug-hunter: bug, wrong, incorrect, fail*, exception, raise, crash, none, null, race condition, deadlock, thread-safe, security, vulnerab*, injection, unsafe, leak, edge case, infinite loop, off-by-one, unhandled, broken, won't/doesn't work
- performance: performance, slow, fast, speed, optimi*, cache, memory, O(, quadratic, complexity, benchmark, overhead, latency, expensive, allocat*, lazy, n+1
- architecture: architectur*, design, public api, backward compat*, deprecat*, breaking change, interface, abstraction, coupl*, circular import, refactor, separation of concerns, responsibilit*, extensib*, dependency
- style: nit, typo, naming, rename, docstring, format, whitespace, indent, type hint, annotation, readab*, spelling, lint, consisten*, unused, simplif*, pythonic, f-string, comment

Limits that matter when reading the numbers: only comments that a reviewer left on added lines are ground truth, so a correct finding the reviewer did not mention counts as a false positive (precision is a lower bound); the category is heuristic, so the `location-only` policy is the more trustworthy of the two; the PR author's own replies and general discussion are excluded. `evaluation/CORPUS.md` records the corpus SHA-256, build date and counts.

For academic reporting, record the commit SHA, corpus hash, model identifier, analyzer versions, environment, prompt version and raw JSON output. Cost fields remain null unless observed provider usage and an explicit price schedule are available; do not infer or fabricate them.

## Recorded runs

- `evaluation/corpus.jsonl` (42 cases, 73 findings, SHA-256 in `evaluation/CORPUS.md`) with `evaluation/repos/` source snapshots.
- `evaluation/results_static.json`: raw static-only run (Ruff + Semgrep, no LLM). Agent strategies are listed under `strategies_not_run`. Read the numbers with the limits above: they are a baseline on weak, comment-derived labels, not a verdict.
