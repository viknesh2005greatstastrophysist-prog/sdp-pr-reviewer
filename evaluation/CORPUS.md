# Evaluation corpus card

- Built (UTC): 2026-11-13 22:23
- Corpus SHA-256 (evaluation/corpus.jsonl): `6ce5d9d8a7fce34ef823154222adf53f5c2ac304b17d3abbf83116bc10d71d11`
- Cases: 144; human-comment findings: 534
- Source: public merged PRs; inline human review comments on added `.py` lines; every case has at least 2 kept comments.
- Split: whole repositories, seeded hash order (`sdp-pr-reviewer-split-v1`), first half `dev`, second half `test`. Rules and thresholds may be tuned on `dev` only; `test` is reported, never tuned on.
- Location is human. Heuristic category is a keyword label; the LLM classifier (see RESULTS.md) adds actionable/category judgements. Neither is a human label.
- Comments later addressed (outdated or line changed by a later commit): 403 of 534
- LLM-classified comments: 534; actionable: 413 (77.3%)

| Repo | Split | Cases | Findings |
| --- | --- | ---: | ---: |
| Kludex/starlette | test | 10 | 32 |
| aio-libs/aiohttp | dev | 10 | 35 |
| encode/httpx | dev | 10 | 45 |
| fastapi/fastapi | dev | 10 | 37 |
| fastapi/sqlmodel | dev | 6 | 23 |
| pallets/click | test | 10 | 27 |
| pallets/flask | test | 8 | 20 |
| pallets/jinja | test | 10 | 27 |
| pallets/werkzeug | test | 10 | 29 |
| psf/black | dev | 10 | 28 |
| psf/requests | dev | 10 | 46 |
| pydantic/pydantic | test | 10 | 44 |
| pytest-dev/pytest | dev | 10 | 44 |
| python-poetry/poetry | test | 10 | 34 |
| scikit-learn/scikit-learn | dev | 10 | 63 |

| Split | Cases | Findings |
| --- | ---: | ---: |
| dev | 76 | 321 |
| test | 68 | 213 |

| Heuristic category | Findings |
| --- | ---: |
| architecture | 13 |
| bug-hunter | 73 |
| performance | 11 |
| style | 437 |
