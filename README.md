# PRISM: evidence-grounded multi-agent pull request reviewer

PRISM reviews Python pull requests on GitHub. It fetches the diff and the changed `.py` files
into a temporary, isolated checkout, runs two deterministic analysers (Ruff and Semgrep) and
four LLM specialists (bug-hunter, performance, style and syntax, architecture) against the
changed lines, and keeps only the findings whose location it can check against the diff. A
second model call, the verification pass, can drop agent findings the diff does not support.
Findings are deduplicated and ranked by severity and confidence, and GitHub suggestion blocks
are offered only when they pass syntax and line-range checks. Reviews and jobs are stored in
PostgreSQL (SQLite for local use), shown in a web dashboard, and can be posted as an advisory
GitHub `COMMENT` review. PRISM never approves, merges or runs code from the pull request.

Live demo: https://sdp-pr-reviewer.onrender.com (free tier; the first request after an idle
period can take up to a minute to start). Demo accounts are given to evaluators separately.

## Features mapped to the final objectives

| # | Objective | Where it is implemented |
|---|---|---|
| 1 | Web dashboard for running and reading reviews | `src/pr_reviewer/static/`, `api.py` (`/`, `/dashboard/*`) |
| 2 | GitHub OAuth sign-in | `auth.py`, `api.py` (`/auth/*`, `/me/repos*`) |
| 3 | Real pull requests, diffs and line maps | `github.py`, `parser.py`, `service.py` |
| 4 | Ruff, Semgrep and four specialist agents | `analyzers.py`, `semgrep_rules.yml`, `agents.py`, `orchestrator.py` |
| 5 | Line-grounded evidence for every finding | `agents.py` (added-line filter), `analyzers.py` (added lines only), `github.py` (publish skips off-diff lines) |
| 6 | Deduplicate and rank by severity and confidence | `aggregator.py`, `review_mode.py` |
| 7 | Validated GitHub suggestions | `refactor.py` |
| 8 | Dashboard summaries, history, agent results and job status | `jobs.py`, `database.py`, `api.py` (`/jobs*`, `/reviews/*`, `/history/*`) |
| 9 | Advisory comments only when enabled; never approve or merge | `service.py`, `github.py` (`publish_review`, event `COMMENT`) |
| 10 | Secure webhooks, fault tolerance, persistence, Docker, tests and evaluation | `api.py` (webhook), `agents.py` (`run_agent`), `database.py`, `Dockerfile`, `docker-compose.yml`, `tests/`, `evaluation.py` |

Each objective is traced to its tests in [docs/DELIVERABLES.md](docs/DELIVERABLES.md).

## Quick start

Requires Python 3.11 or newer.

Local virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env               # fill only the values you use
uvicorn pr_reviewer.api:app --reload
```

Open http://localhost:8000. Without `DATABASE_URL` the app uses `./review_history.db` (SQLite).
Without any sign-in method configured, all stored reviews are visible, which is meant for local
development only.

Docker Compose (API plus PostgreSQL 16):

```bash
cp .env.example .env
docker compose up --build
curl http://localhost:8000/health
```

Compose reads `.env` and overrides `DATABASE_URL` to point at its PostgreSQL service. The
PostgreSQL password in `docker-compose.yml` is a local development value.

Review a local diff from the command line (no server needed):

```bash
pr-reviewer examples/demo.diff --repo examples/demo_repo --offline
```

On Windows, after local setup, `.\scripts\demo.ps1` runs the same demonstration.

## Configuration

Settings are read from the environment in `src/pr_reviewer/settings.py`. Values in `.env` are
loaded by Docker Compose; for `uvicorn` on the host, export them or load them with your shell.

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_TOKEN` | unset | Server-side GitHub token for webhook, demo and CLI-style reviews. Takes precedence over the GitHub App. |
| `GITHUB_APP_ID` | unset | GitHub App id. Used with `GITHUB_PRIVATE_KEY` when no `GITHUB_TOKEN` is set. |
| `GITHUB_PRIVATE_KEY` | unset | GitHub App private key (PEM). Literal `\n` sequences are converted to newlines. |
| `GITHUB_WEBHOOK_SECRET` | unset | HMAC-SHA256 secret for `POST /webhooks/github`. Without it the endpoint returns 503. |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub REST base URL (for GitHub Enterprise Server). |
| `GITHUB_OAUTH_URL` | `https://github.com` | Host for the OAuth authorize and token endpoints. Not in `.env.example`. |
| `GITHUB_OAUTH_CLIENT_ID` | unset | OAuth App client id. Sign-in is enabled when this and the secret are set. |
| `GITHUB_OAUTH_CLIENT_SECRET` | unset | OAuth App client secret. |
| `GITHUB_OAUTH_SCOPES` | `repo read:user` | Scopes requested at sign-in. See the scope trade-off in `docs/SECURITY.md`. |
| `SESSION_SECRET` | unset | Encrypts stored GitHub tokens and keys the session-id hash. If unset, a random secret is used and sessions end on restart. Set it explicitly. |
| `PUBLIC_BASE_URL` | `RENDER_EXTERNAL_URL`, else `http://localhost:8000` | Public origin. Used for the OAuth callback, same-origin checks and the `Secure` cookie flag (https only). |
| `DEMO_ACCOUNTS` | empty | Demo sign-in accounts as `username:hash` pairs, comma separated. See Demo accounts. |
| `LLM_API_KEY` | unset | Key for an OpenAI-compatible provider. Without it the specialists and verifier are offline and the review is static-only. |
| `LLM_MODEL` | `gpt-4.1-mini` | Model id sent to the provider. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL. |
| `AGENT_TIMEOUT` | `45` | Seconds allowed per specialist before it is recorded as `timed_out`. |
| `MIN_CONFIDENCE` | `0` | Findings below this confidence (0 to 1) are hidden with a warning. |
| `DATABASE_URL` | `sqlite:///./review_history.db` | SQLAlchemy URL. `postgres://` and `postgresql://` are converted to the psycopg driver. |
| `REVIEW_CONCURRENCY` | `2` | Review jobs running at once (minimum 1). |
| `PUBLISH_REVIEWS` | `false` | Server permission to publish `COMMENT` reviews. Each request must still opt in. |
| `WEBHOOK_AUTO_PUBLISH` | `false` | Also publish webhook-triggered reviews. Needs `PUBLISH_REVIEWS=true`. |
| `REVIEW_MODE` | `focused` | `focused` or `full`. Any other value is a configuration error. Not in `.env.example`. |
| `REVIEW_VERIFY` | `true` | Run the verification pass in focused mode when an LLM is configured. Not in `.env.example`. |
| `REVIEW_TOP_K` | `0` | Maximum findings shown per pull request (0 means no cap). Not in `.env.example`. |
| `PORT` | `8000` | Port the Docker `CMD` listens on. Read by the Dockerfile, not by `settings.py`. |

`HOME`, `SEMGREP_SEND_METRICS` and `RUFF_CACHE_DIR` are set in `render.yaml` and
`docker-compose.yml` so the analysers can write caches and stay offline. The application does
not read them.

## GitHub OAuth App setup

Create the app at GitHub, Settings, Developer settings, OAuth Apps, New OAuth App.

- Homepage URL: the value of `PUBLIC_BASE_URL`, for example `https://sdp-pr-reviewer.onrender.com`.
- Authorization callback URL: `PUBLIC_BASE_URL/auth/github/callback`.
- Scopes: the default `repo read:user` is requested at sign-in. The `repo` scope is broad because
  OAuth has no narrower scope that reads private repositories. `docs/SECURITY.md` describes the
  trade-off and the GitHub App alternative.

Put the client id and secret in `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`. The
browser holds only an opaque, HttpOnly session cookie. The GitHub token is encrypted on the server.

## Demo accounts

Demo accounts let evaluators sign in without GitHub. A demo user cannot browse repositories. They
enter `owner`, `repo` and a pull request number, and the review uses the server's GitHub
credentials.

1. Generate a password hash. The command prompts twice for a password of at least 12 characters
   and prints only the hash:

   ```bash
   pr-reviewer-hash-password
   ```

2. Set `DEMO_ACCOUNTS` to comma-separated `username:hash` pairs, for example
   `DEMO_ACCOUNTS=evaluator:pbkdf2_sha256$600000$<salt>$<hash>`. Hashes must be PBKDF2-SHA256
   with at least 600,000 iterations. Malformed entries are skipped.

Passwords are never written to the repository, the environment file or this README. The Render
blueprint sets `DEMO_ACCOUNTS` to hashes only.

## Deploying on Render

`render.yaml` is a Render Blueprint. It defines one Docker web service (`sdp-pr-reviewer`, free
plan, health check `/health`) and one free PostgreSQL database (`sdp-pr-reviewer-db`).

Values the blueprint generates:

- `SESSION_SECRET`
- `GITHUB_WEBHOOK_SECRET`
- `DATABASE_URL` (taken from the database's connection string)

Values the owner pastes into the Render dashboard (marked `sync: false`, never committed):

- `GITHUB_OAUTH_CLIENT_ID`
- `GITHUB_OAUTH_CLIENT_SECRET`
- `GITHUB_TOKEN`
- `LLM_API_KEY`

The blueprint also sets `DEMO_ACCOUNTS` (hashes), `LLM_MODEL`, `LLM_BASE_URL` (a Gemini
OpenAI-compatible endpoint), `AGENT_TIMEOUT=120`, `PUBLISH_REVIEWS=false` and `REVIEW_CONCURRENCY=2`.
`PUBLIC_BASE_URL` is not set; Render provides `RENDER_EXTERNAL_URL`, which the app uses.

The free Render PostgreSQL database expires after 30 days, and review history and sessions are
lost with it. Treat the free tier as a demonstration, and export anything worth keeping.

## Language-model fallback

`LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` select the primary OpenAI-compatible provider (the live
server uses Gemini). When `LLM_FALLBACK_API_KEY` is set, a call that the primary rejects for rate
limits or exhausted quota is retried on the fallback provider (default: Groq,
`openai/gpt-oss-120b`, which supports strict JSON-schema output). Requests use a strict-mode
schema so both providers accept them. If both fail, that one agent is marked failed and the rest
of the review completes. The evaluation never uses the fallback: each benchmark runs on one model.

## Review modes and the verification pass

- **Focused (default).** Shows findings that indicate a likely defect: Semgrep rules, and Ruff
  findings from the security (S), bugbear (B), pyflakes (F) and syntax (E9) families, excluding
  cosmetic members. Agent findings must reach confidence 0.7. At most five findings are shown per
  file. `REVIEW_TOP_K` optionally caps each pull request.
- **Full.** Shows every finding from every analyser and agent, subject to `MIN_CONFIDENCE`.

In focused mode with an LLM configured and `REVIEW_VERIFY=true`, one extra model call judges the
agent findings against the changed code. Ungrounded findings are dropped and the review records
how many. Static findings are never sent to the verifier. If the verifier call fails, the findings
are kept and a warning is added. The verifier can only remove findings.

Focused mode uses principled defaults (defect-indicating rule families, a confidence floor and a
per-file cap). Tuning them on the development split produced no measurable signal on the cases
that completed, so the defaults were kept; nothing was tuned on the test split. See
`evaluation/RESULTS.md` and `docs/EVALUATION.md`.

## Publishing policy

- Reviews are never approved, merged or closed by PRISM. The only GitHub event sent is `COMMENT`.
- Publishing needs two things. The server must have `PUBLISH_REVIEWS=true`. The request must opt
  in with `"publish": true` (dashboard or API), or the webhook path must also set
  `WEBHOOK_AUTO_PUBLISH=true`.
- If publishing fails, the review is still stored and the error is recorded on the job.
- Only validated suggestions and comments on added lines are posted.

## API endpoints

The POST endpoints `/review`, `/github/review`, `/auth/password` and `/auth/logout` reject a
cross-site `Origin` header. Review data is restricted to signed-in viewers whenever a sign-in
method is configured.

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /` | none | Dashboard page |
| `GET /assets/*` | none | Dashboard JavaScript and CSS |
| `GET /health` | none | Liveness and database check |
| `GET /dashboard/status` | none | Capability state (no secret values) |
| `GET /dashboard/demo-cases` | none | Bundled demonstration cases |
| `POST /dashboard/demo` | none, rate limited | Run a bundled case through the full pipeline; body `{"case_id": "..."}` optional |
| `GET /auth/status` | none | Whether signed in, and which sign-in methods exist |
| `GET /auth/github/login` | none | Start GitHub OAuth (503 if not configured) |
| `GET /auth/github/callback` | OAuth state cookie | Complete OAuth and set the session cookie |
| `POST /auth/password` | same origin | Demo account sign-in; throttled per user and per address |
| `POST /auth/logout` | same origin | End the session |
| `GET /me/repos` | GitHub sign-in | List the signed-in user's repositories |
| `GET /me/repos/{owner}/{repo}/pulls` | GitHub sign-in | List pull requests; `state` is `open`, `closed` or `all` |
| `POST /review` | viewer, same origin, rate limited | Review a supplied diff synchronously; no repository, so no Ruff or Semgrep |
| `POST /github/review` | same origin | Queue a review of a pull request; returns 202 with the job; body `{"owner", "repo", "number", "publish"}` |
| `GET /jobs` | viewer | Recent jobs; optional `limit` and `repository` |
| `GET /jobs/{job_id}` | viewer | Job status, and the result once completed |
| `GET /reviews/{review_id}` | viewer | Stored review |
| `GET /history/{owner}/{repo}` | viewer | Stored reviews for a repository; optional `limit` |
| `POST /webhooks/github` | HMAC signature | Handles `pull_request` events (opened, reopened, synchronize, ready_for_review); answers `ping` |

Rate limits are 20 requests per minute per client address on `/review` and `/dashboard/demo`.

## CLI commands

Installed by `pip install -e .` (`[project.scripts]` in `pyproject.toml`).

| Command | Purpose |
|---|---|
| `pr-reviewer DIFF [--repo PATH] [--offline]` | Review a diff file and print JSON. `--repo` points at an isolated checkout for Ruff and Semgrep. `--offline` disables the LLM specialists. Never publishes. |
| `pr-reviewer-hash-password` | Prompt for a password and print its `DEMO_ACCOUNTS` hash |
| `pr-reviewer-evaluate CORPUS --output FILE` | Run the evaluation. Options include `--strategies`, `--subset`, `--limit`, `--checkpoint`, `--redo-failed`, `--no-verify`, `--sequential-agents`, `--max-passes` and `--retry-wait`. |
| `pr-reviewer-build-corpus [REPOS...]` | Build a corpus from merged public pull requests. Options include `--max-prs`, `--max-cases-per-repo`, `--skip-merged`, `--merge` and `--output`. |
| `pr-reviewer-label-corpus CORPUS` | Label corpus comments with an LLM classifier. Options include `--csv`, `--card`, `--stats` and `--delay`. |
| `pr-reviewer-score` | Score saved predictions offline (`tune` and `score` subcommands). |

Run any command with `--help` for the full option list.

## Evaluation

The evaluation compares static-only, single-agent and hybrid (four-specialist) strategies on a
held-out, human-checked corpus. Results are in [evaluation/RESULTS.md](evaluation/RESULTS.md) (being
finalised), and the protocol, labelling and limitations are in [docs/EVALUATION.md](docs/EVALUATION.md).
Corpus labels produced by an LLM are not yet verified by the team, and the README does not
quote numbers until the run is final.

## Testing

```bash
pytest -q                                   # unit and integration tests
ruff check src tests
ruff format --check src tests
pytest -q --cov=pr_reviewer --cov-report=term-missing
semgrep scan --config src/pr_reviewer/semgrep_rules.yml src tests
```

Continuous integration (`.github/workflows/ci.yml`) runs the Ruff checks, the test suite with
coverage and the Semgrep scan on every push and pull request. Semgrep has no native Windows
package; use WSL or Docker for it. Tests cover the OAuth flow, sessions, demo accounts, webhook
signatures, job lifecycle, publishing gates, suggestion validation and the evaluation tools.
The test file names are listed in `docs/DELIVERABLES.md`.

## Project layout

```text
src/pr_reviewer/
  api.py              FastAPI routes, dashboard, webhook, OAuth routes, rate limits
  auth.py             OAuth flow, encrypted server-side sessions, demo password hashing
  jobs.py             Background job manager with bounded concurrency
  service.py          Fetch, review, store and optionally publish one pull request
  github.py           GitHub REST client, webhook signature check, App installation tokens
  parser.py           Unified diff parser and GitHub patch-position line map
  context.py          Python AST context around changed symbols (never imports repo code)
  analyzers.py        Ruff and Semgrep adapters, filtered to added lines
  semgrep_rules.yml   Project Semgrep rules
  agents.py           Four specialist agents, per-agent timeout and fault boundary
  orchestrator.py     LangGraph fan-out and fan-in of the specialists
  llm.py              OpenAI-compatible structured-output client with retries
  verifier.py         Verification pass for agent findings (focused mode)
  aggregator.py       Deduplication, ranking and executive summary
  review_mode.py      Focused and full review modes
  refactor.py         Validation of GitHub suggestion blocks
  pipeline.py         Diff-to-result pipeline shared by all entry points
  database.py         SQLAlchemy models and the review, job and session store
  models.py           Pydantic models for findings, results and references
  settings.py         Environment configuration
  ratelimit.py        In-memory per-address rate limiter
  demos.py            Bundled deterministic demonstration cases
  evaluation.py       Evaluation runner and metrics
  corpus.py           Corpus builder from public pull requests
  labeling.py         LLM labelling of corpus comments
  injection.py        Injected-defect benchmark generator
  scoring.py          Offline scoring, tuning and adjudication sample
  cli.py              pr-reviewer command
  cli_auth.py         pr-reviewer-hash-password command
  static/             Dashboard (index.html, dashboard.js, dashboard.css)
tests/                Test suite
docs/                 Architecture, security, quality, evaluation and deliverable notes
evaluation/           Corpus, injected-defect benchmark and cards
examples/             Demonstration diff and repository
scripts/demo.ps1      Windows demonstration script
```

## Team and guide

Team 61, final-year project, KLE Technological University.

- Naveen Rajdev
- Pooja P
- Vikneshwaran Marimuthu
- Vaishnavi Pagad

Project guide: Prashant Narayankar.
