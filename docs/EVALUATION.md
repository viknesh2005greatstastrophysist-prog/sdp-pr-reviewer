# Reproducible evaluation

Use a held-out JSONL corpus. Each line must contain `case_id`, the exact unified `diff`, an optional isolated `repo_path`, and `human_findings` using the production `Finding` schema. The corpus must be separated from prompt or rule development. At least two reviewers should independently label each pull request and adjudicate disagreements before the file is frozen.

Run:

```bash
pr-reviewer-evaluate data/held_out.jsonl --output results/evaluation.json
```

With `LLM_API_KEY` configured, the runner compares static-only, one generalist agent, and the hybrid four-specialist pipeline. Without credentials it reports static-only rather than pretending that offline placeholders are agents. Matching is one-to-one on category and path within two lines. The report contains confusion counts, precision, recall, F1, mean latency and each false positive for manual adjudication. An empty corpus is rejected. No example metrics are included because no human-reviewed held-out corpus was supplied.

For academic reporting, record the commit SHA, corpus hash, model identifier, analyzer versions, environment, prompt version and raw JSON output. Cost fields remain null unless observed provider usage and an explicit price schedule are available; do not infer or fabricate them.
