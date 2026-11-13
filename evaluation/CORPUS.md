# Evaluation corpus card

- Built (UTC): 2026-11-13 22:09
- Corpus SHA-256 (evaluation/corpus.jsonl): `ac878ccde23706b79795203a0b9c882fe0b396f42256cfdf9997b0ef00367fb6`
- Cases: 2; human-comment findings: 3
- Source: public merged PRs; inline human review comments on added `.py` lines; every case has at least 2 kept comments.
- Split: whole repositories, seeded hash order (`sdp-pr-reviewer-split-v1`), first half `dev`, second half `test`. Rules and thresholds may be tuned on `dev` only; `test` is reported, never tuned on.
- Location is human. Heuristic category is a keyword label; the LLM classifier (see RESULTS.md) adds actionable/category judgements. Neither is a human label.
- Comments later addressed (outdated or line changed by a later commit): 1 of 3
- LLM-classified comments: 2; actionable: 1 (50.0%)

| Repo | Split | Cases | Findings |
| --- | --- | ---: | ---: |
| o/r | dev | 2 | 3 |

| Split | Cases | Findings |
| --- | ---: | ---: |
| dev | 2 | 3 |

| Heuristic category | Findings |
| --- | ---: |
| bug-hunter | 1 |
| style | 2 |
