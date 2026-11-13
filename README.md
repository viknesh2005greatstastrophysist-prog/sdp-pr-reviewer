# Evidence-Grounded Multi-Agent Pull Request Reviewer

A production-oriented academic reviewer for Python pull requests. It combines deterministic Ruff/Semgrep evidence with four concurrent LLM specialists, validates every reported location against the diff, consolidates overlap, creates bounded GitHub suggestions, stores review history and can publish an advisory GitHub review. It never approves, merges or executes pull-request code.

## Capabilities

- GitHub REST ingestion, GitHub App or token authentication, HMAC-validated webhooks, safe changed-file materialization, unified-diff mapping and AST context extraction.
- Ruff and local-rule Semgrep adapters filtered to added lines.
- Bug-Hunter, Performance, Style/Syntax and Architecture/Scalability agents with Pydantic structured output and OpenAI-compatible APIs.
- LangGraph fan-out/fan-in orchestration, per-agent timeout/fault isolation, severity ordering and evidence-aware deduplication.
- Python syntax-checked, added-line-only GitHub suggestion blocks.
- `COMMENT` reviews with inline findings and executive summaries; publishing is opt-in.
- PostgreSQL-backed review history plus a SQLite default for local use.
- Held-out JSONL evaluation of static-only, single-agent and hybrid approaches without fabricated metrics.
- Pytest, Ruff, Docker Compose, CI, Mermaid UML and an offline demo.

## Local setup

Python 3.11 or newer is required.

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install -e ".[dev,postgres]"
pytest -q
ruff check src tests
ruff format --check src tests
```

Semgrep does not ship a native Windows package; use Docker/WSL for the full static stack. Ruff and all other tests run natively. Copy `.env.example` to `.env` and fill only the credentials you use.

## Run and demonstrate

Review a local diff against an isolated or trusted checkout:

```bash
pr-reviewer examples/demo.diff --repo examples/demo_repo --offline
```

On Windows after local setup:

```powershell
.\scripts\demo.ps1
```

Start the API and PostgreSQL:

```bash
docker compose up --build
curl http://localhost:8000/health
```

Open `http://localhost:8000` for the interactive PRISM dashboard. Demo mode runs a
bundled trusted pull request through the complete local pipeline. Live mode accepts a
GitHub owner, repository and pull-request number and uses the configured credentials.
The Workflow, Findings, History and Configuration views expose every major project path
without returning secret values to the browser.

Useful endpoints:

- `POST /review` with `{"diff": "..."}` reviews diff data without local analyzers.
- `POST /github/review` with `{"owner":"org","repo":"project","number":42}` runs the complete GitHub path.
- `POST /webhooks/github` accepts signed `pull_request` events.
- `GET /history/{owner}/{repo}` lists stored reviews.

## GitHub App setup

Create a GitHub App with pull-request read/write and repository-contents read permission. Subscribe to pull-request events and set its webhook URL to `/webhooks/github`. Configure `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY` and `GITHUB_WEBHOOK_SECRET`. A short-lived installation token is created for each repository. `GITHUB_TOKEN` is supported for local development and takes precedence when present.

Set `PUBLISH_REVIEWS=true` only after verifying the target repository. The publisher always sends `event: COMMENT`; there is no approval or merge code path.

## LLM configuration

Set `LLM_API_KEY`, `LLM_MODEL` and optionally `LLM_BASE_URL`. The client uses the OpenAI-compatible `/chat/completions` JSON-schema response format, temperature zero, retry/backoff for rate limits and server failures, and per-agent timeouts. Without credentials the static pipeline remains functional and agent runs are explicitly offline.

## Evaluation

Prepare a held-out human-labelled JSONL corpus, then run:

```bash
pr-reviewer-evaluate data/held_out.jsonl --output results/evaluation.json
```

See [evaluation protocol](docs/EVALUATION.md), [architecture and UML](docs/ARCHITECTURE.md), [security model](docs/SECURITY.md), and [deliverable mapping](docs/DELIVERABLES.md). No evaluation scores are claimed because the starter did not include a held-out human-reviewed corpus.
