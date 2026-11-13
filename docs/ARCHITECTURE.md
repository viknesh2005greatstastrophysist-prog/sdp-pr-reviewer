# Architecture and UML

PRISM is an advisory reviewer. A dashboard request, an API request or a signed GitHub webhook
identifies one pull request. The GitHub client fetches the metadata, the unified diff and the
contents of the changed `.py` files. Repository files are written as inert data into a
temporary directory. Ruff and Semgrep read only that directory. Python context extraction uses
`ast.parse` and never imports repository code. Four specialists then run concurrently on the
patches and the bounded context. Their findings are checked against the added-line map,
deduplicated and ranked, and in focused mode a verification pass can drop unsupported agent
findings. The result is stored in PostgreSQL or SQLite. Only then can it be posted as a `COMMENT`
review, and only when publishing is enabled.

## Components

```mermaid
flowchart LR
  BR["Browser dashboard"]
  CLI["pr-reviewer CLI"]
  GH["GitHub REST and OAuth"]
  subgraph API ["FastAPI app (api.py)"]
    OAUTH["OAuth and sessions (auth.py)"]
    ROUTES["Review, job and history routes"]
    HOOK["Webhook handler (HMAC check)"]
  end
  JOBS["JobManager (jobs.py) with bounded concurrency"]
  SVC["ReviewService (service.py)"]
  GHC["GitHubClient (github.py)"]
  PARSE["Diff parser and line map (parser.py)"]
  CTX["Python AST context (context.py)"]
  STATIC["Ruff and Semgrep (analyzers.py)"]
  ORCH["LangGraph fan-out (orchestrator.py)"]
  AGENTS["Four specialists (agents.py, llm.py)"]
  VER["Verifier, focused mode only (verifier.py)"]
  AGG["Deduplicate and rank (aggregator.py)"]
  MODE["Focused or full filter (review_mode.py)"]
  REF["Suggestion validator (refactor.py)"]
  STORE["ReviewStore (database.py)"]
  DB[("PostgreSQL or SQLite")]
  PIPE["review_diff (pipeline.py)"]

  BR -->|"sign-in and review requests"| ROUTES
  BR -->|"OAuth redirect"| OAUTH
  CLI -->|"diff file, offline or with repo"| PIPE
  OAUTH -->|"code exchange, user profile"| GH
  OAUTH -->|"encrypted session"| STORE
  ROUTES --> JOBS
  ROUTES -->|"POST /review, no repository"| PIPE
  HOOK -->|"queued job, deduplicated by delivery id"| JOBS
  JOBS --> SVC
  SVC --> GHC
  GHC <-->|"metadata, diff, contents, publish"| GH
  SVC --> PARSE
  SVC --> PIPE
  PIPE --> CTX
  PIPE --> STATIC
  PIPE --> ORCH
  PARSE --> ORCH
  ORCH --> AGENTS
  AGENTS --> AGG
  STATIC --> AGG
  AGG --> VER
  VER --> MODE
  MODE --> REF
  REF --> STORE
  STORE --> DB
  SVC -->|"publish if permitted"| GHC
```

Two paths reach the same pipeline. `review_diff` in `pipeline.py` takes a diff, an optional
checkout and a list of agents. The CLI and the synchronous `POST /review` call it directly.
`ReviewService` wraps it for pull requests: fetch, materialize, review, store and publish.

## Review job sequence (dashboard)

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser
  participant A as FastAPI api.py
  participant G as GitHub
  participant D as ReviewStore
  participant J as JobManager
  participant S as ReviewService
  B->>A: GET /auth/github/login
  A-->>B: 302 to GitHub with state cookie
  B->>G: Authorize the OAuth App
  G-->>B: 302 to /auth/github/callback with code and state
  B->>A: GET /auth/github/callback
  A->>A: Compare the state cookie with the state query value
  A->>G: Exchange the code for an access token
  A->>G: GET /user
  A->>D: Save session (id hashed, GitHub token encrypted)
  A-->>B: 302 to dashboard with HttpOnly session cookie
  B->>A: POST /github/review (Origin checked)
  A->>D: create_job (an active job for the same pull request and head returns the existing one)
  A-->>B: 202 with job
  A->>J: submit as background task
  J->>D: status running
  J->>S: review with the signed-in user's token
  S->>G: Pull request metadata, diff, changed .py contents
  S->>S: Parse diff, run Ruff and Semgrep on temp checkout
  S->>S: Run specialists, aggregate, verify (focused), apply review mode
  S->>D: save review
  opt publish requested and PUBLISH_REVIEWS is true
    S->>G: POST review with event COMMENT
  end
  J->>D: status completed, or failed with error text
  loop until finished
    B->>A: GET /jobs/job_id
    A-->>B: status and, when completed, the result
  end
```

## Webhook sequence

```mermaid
sequenceDiagram
  autonumber
  participant G as GitHub
  participant A as FastAPI api.py
  participant D as ReviewStore
  participant J as JobManager
  participant S as ReviewService
  G->>A: POST /webhooks/github with signature and delivery id headers
  A->>A: HMAC-SHA256 over the raw body, compared in constant time
  alt secret missing (503) or signature invalid (401)
    A-->>G: rejected
  else ping event
    A-->>G: pong
  else other event, unsupported action, or draft pull request
    A-->>G: ignored with reason
  else pull_request review action
    A->>D: create_job with delivery id
    alt delivery id already used
      A-->>G: duplicate with existing job id
    else new job
      A->>J: submit with no user token
      A-->>G: queued with job id
      J->>S: review with GITHUB_TOKEN or GitHub App installation token
      S->>G: Pull request metadata, diff, changed .py contents
      S->>S: Analysis, specialists, aggregate, verify (focused), apply review mode
      S->>D: save review
      opt PUBLISH_REVIEWS and WEBHOOK_AUTO_PUBLISH are both true
        S->>G: POST review with event COMMENT
      end
      J->>D: status completed or failed
    end
  end
```

A failed delivery releases its delivery id, so a redelivery starts a fresh job instead of
replaying the failure. Webhook reviews carry no requester, so they are visible to demo accounts
and to the unauthenticated local mode only.

## Main classes

```mermaid
classDiagram
  class ReviewService
  class JobManager
  class ReviewStore
  class GitHubClient
  class StaticAnalyzer
  class PythonContextExtractor
  class LangGraphOrchestrator
  class BaseReviewAgent
  class LLMReviewAgent
  class OfflineAgent
  class OpenAICompatibleClient
  class RefactoringEngine
  class Settings
  ReviewService --> GitHubClient
  ReviewService --> StaticAnalyzer
  ReviewService --> PythonContextExtractor
  ReviewService --> LangGraphOrchestrator
  ReviewService --> RefactoringEngine
  ReviewService --> ReviewStore
  ReviewService --> Settings
  JobManager --> ReviewStore
  JobManager --> ReviewService
  LangGraphOrchestrator o-- BaseReviewAgent
  LLMReviewAgent --|> BaseReviewAgent
  OfflineAgent --|> BaseReviewAgent
  LLMReviewAgent --> OpenAICompatibleClient
```

`verify_findings` in `verifier.py` is a function, not a class. It takes the same
`OpenAICompatibleClient` used by the specialists.

## Fault tolerance and state

- Each specialist runs inside `run_agent`. A timeout (`AGENT_TIMEOUT`), an exception or a quota
  failure is recorded as an `AgentRun` with status `timed_out` or `failed`, and the other
  specialists still contribute. Failures appear as warnings on the result.
- If the verification call fails, its findings are kept and a warning is added. Verification can
  only remove findings.
- If LangGraph cannot be imported, the pipeline falls back to concurrent `asyncio` dispatch and
  records a warning.
- Job failures are written to the job record with the error text. At startup, jobs left active by
  a previous process are marked failed (`fail_interrupted_jobs`), so no job stays in progress forever.
- Persistence has three tables: `review_history` (review payload, repository, pull number, head
  commit and requester), `review_jobs` (status, trigger, publish flags, errors and delivery id) and
  `user_sessions` (hashed session id, login, encrypted token and expiry). `create_schema` adds the
  requester column to databases created before it existed.
- Focused and full review modes apply after aggregation. The verifier runs only in focused mode.

## Security boundaries

Repository content, diffs and comments are treated as data. Analysers are run with fixed argument
arrays and `shell=False`, and repository configuration files are excluded, so a pull request
cannot switch off the tools that review it. Credentials are read from the environment and never
returned to the browser. The full model, including the OAuth scope trade-off, is in
[SECURITY.md](SECURITY.md).
