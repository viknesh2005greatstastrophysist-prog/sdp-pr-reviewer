# Evaluation corpus card

- Built (UTC): 2026-11-13 08:50
- Corpus SHA-256 (evaluation/corpus.jsonl): `edaea081c10c13214c0187eac7677ff33e2f22cbfc67f6af995da5436e80239e`
- Cases: 42; human-comment findings: 73
- Source: public merged PRs; inline human review comments on added `.py` lines.
- Labels: location is human; category is a keyword heuristic (`label_source = "heuristic-category"`), not a human label.

| Repo | Cases | Findings |
| --- | ---: | ---: |
| encode/httpx | 7 | 11 |
| fastapi/fastapi | 7 | 10 |
| pallets/flask | 7 | 9 |
| psf/requests | 7 | 8 |
| pydantic/pydantic | 7 | 26 |
| python-poetry/poetry | 7 | 9 |

| Heuristic category | Findings |
| --- | ---: |
| architecture | 3 |
| bug-hunter | 17 |
| style | 53 |
