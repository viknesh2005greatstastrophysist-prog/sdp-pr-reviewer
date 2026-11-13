# Architecture and UML

The reviewer is advisory. A webhook or explicit API request identifies a pull request. The GitHub adapter fetches the diff and changed file contents as inert data into a temporary directory. Ruff and Semgrep inspect only that isolated materialization. Python context extraction uses `ast.parse`; it never imports repository modules. Four LangGraph nodes call independent structured-output specialists concurrently. The aggregator validates locations, consolidates overlap and ranks findings before the GitHub adapter posts a `COMMENT` review.

## Component diagram

```mermaid
flowchart LR
  GH[GitHub REST API] --> WH[FastAPI webhook]
  WH --> ING[PR ingestion and safe materializer]
  ING --> PARSE[Diff parser and line map]
  ING --> AST[AST context extractor]
  ING --> STATIC[Ruff and Semgrep]
  PARSE --> GRAPH[LangGraph orchestrator]
  AST --> GRAPH
  GRAPH --> BUG[Bug-Hunter]
  GRAPH --> PERF[Performance]
  GRAPH --> STYLE[Style and Syntax]
  GRAPH --> ARCH[Architecture and Scalability]
  BUG --> AGG[Evidence validator and aggregator]
  PERF --> AGG
  STYLE --> AGG
  ARCH --> AGG
  STATIC --> AGG
  AGG --> REF[Suggestion validator]
  REF --> PUB[Inline comments and executive summary]
  PUB --> GH
  AGG --> DB[(PostgreSQL history)]
```

## Review sequence

```mermaid
sequenceDiagram
  participant G as GitHub
  participant A as FastAPI
  participant S as Static layer
  participant L as LangGraph agents
  participant D as PostgreSQL
  G->>A: signed pull_request webhook
  A->>A: verify HMAC-SHA256
  A->>G: fetch metadata, diff and changed contents
  par deterministic analysis
    A->>S: isolated files and added-line map
    S-->>A: Ruff/Semgrep findings
  and specialist analysis
    A->>L: patches plus bounded AST contexts
    L-->>A: typed evidence-grounded findings
  end
  A->>A: deduplicate, rank, validate suggestions
  A->>D: persist result
  opt PUBLISH_REVIEWS=true
    A->>G: COMMENT review only
  end
```

## Main classes

```mermaid
classDiagram
  class ReviewService
  class GitHubClient
  class StaticAnalyzer
  class PythonContextExtractor
  class LangGraphOrchestrator
  class BaseReviewAgent
  class LLMReviewAgent
  class RefactoringEngine
  class ReviewStore
  ReviewService --> GitHubClient
  ReviewService --> StaticAnalyzer
  ReviewService --> PythonContextExtractor
  ReviewService --> LangGraphOrchestrator
  LangGraphOrchestrator o-- BaseReviewAgent
  LLMReviewAgent --|> BaseReviewAgent
  ReviewService --> RefactoringEngine
  ReviewService --> ReviewStore
```
