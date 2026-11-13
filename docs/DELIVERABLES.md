# Deliverable mapping

| Deliverable | Implementation |
|---|---|
| D1 ingestion, parsing, mapping, AST context | `github.py`, `parser.py`, `context.py` |
| D2 Ruff and Semgrep | `analyzers.py`, `semgrep_rules.yml` |
| D3 four functional agents | `agents.py`, `llm.py` |
| D4 concurrent orchestration and aggregation | `orchestrator.py`, `aggregator.py` |
| D5 validated refactoring suggestions | `refactor.py` |
| D6 webhook, comments, summaries | `api.py`, `service.py`, `github.py` |
| D7 reproducible benchmark | `evaluation.py`, `docs/EVALUATION.md` |
| D8 tests, documentation, UML, Docker, demo | `tests/`, `docs/`, `Dockerfile`, `docker-compose.yml`, `scripts/demo.ps1` |

The project is Python-first and GitHub-only. It intentionally does not approve, merge, apply suggestions or execute repository code. Evaluation numbers are produced only from a supplied held-out labelled corpus.
