# Security model

- Repository content, diffs, filenames and comments are untrusted data. Prompts state this boundary and structured outputs are revalidated against the added-line map.
- Changed contents are fetched through GitHub's contents API into a new temporary directory. Paths are resolved beneath that directory and files over 1 MB are skipped.
- Repository code is never imported, executed, tested or passed to a shell. AST parsing is syntax-only. Analyzer subprocesses use argument arrays with `shell=False`.
- Webhooks require an HMAC-SHA256 signature and a configured secret.
- Credentials come from environment variables. GitHub App private keys are short-lived JWT inputs; installation tokens are preferred over personal tokens.
- Suggestions are constrained to added lines, bounded in size, syntax-checked for Python and posted for human action only.
- Publishing is opt-in and always uses GitHub's `COMMENT` event. The application contains no approve or merge operation.
- The container runs as a non-root user, with a read-only filesystem, `no-new-privileges`, and a no-execute temporary filesystem in Compose.

## OAuth sessions

- GitHub sign-in uses the OAuth web flow. The browser holds only a random opaque session id in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` when `PUBLIC_BASE_URL` is https); the server stores a hash of it.
- The GitHub access token is encrypted (Fernet, key derived from `SESSION_SECRET`) before it is stored and is never sent to the browser. Sessions expire after 8 hours; expired rows are purged on every successful sign-in.
- The callback checks a one-time `state` cookie against the `state` query value. On a bad state or a failed code exchange the browser is redirected to `/?auth=error`, never shown raw JSON.
- State-changing browser requests (`POST /auth/password`, `/auth/logout`, `/github/review`, `/review`) reject a cross-site `Origin` header.

## Demo accounts

- `DEMO_ACCOUNTS` holds `username:hash` pairs; hashes are PBKDF2-HMAC-SHA256 with at least 600,000 iterations and a random salt (`pr-reviewer-hash-password`). Plain passwords are never configured or stored. Unknown usernames still run a hash so timing does not reveal which accounts exist.
- Lockout: 5 failures per (username, client address) in 10 minutes, plus 20 failures per client address across usernames. A stranger on another address therefore cannot lock out an evaluator.
- Demo sessions hold no GitHub token and cannot browse repositories.

## Data visibility

- No sign-in method configured (local development): all stored reviews and jobs are visible.
- GitHub user: only rows they requested (`requested_by` = their login).
- Demo account: rows requested by any demo account, plus rows with no requester (webhook, API and server-credential reviews).
- Unauthorised access to a single job or review returns 404, not 403, so ids cannot be probed. A GitHub login equal to a demo account name does not inherit demo rows.
- Reviews stored before this rule have no requester, so only demo accounts (and the unauthenticated local mode) can read them.

## Rate limits

- `POST /dashboard/demo` and `POST /review`: 20 requests per minute per client address, then `429`. In-memory and per process; behind a proxy the address comes from `X-Forwarded-For` (uvicorn `--proxy-headers`), so keep the proxy as the only ingress. `POST /review` additionally needs a signed-in viewer and a same-origin request.

## Analyzer isolation

- A pull request cannot configure the tools that review it. Only `.py` files are fetched into the checkout, so `ruff.toml`, `pyproject.toml`, `.semgrepignore` and `.gitignore` from the PR never exist there.
- Ruff runs with `--isolated` (rule selection comes from the explicit command line only). Semgrep runs with `--x-ignore-semgrepignore-files` and `--no-git-ignore`. `# noqa` / `# nosemgrep` comments in the changed code are still honoured by the tools.

## OAuth scope trade-off

- Sign-in requests the `repo` scope because GitHub OAuth has no narrower scope that reads private repositories and pull requests. That token can read and write the user's repositories, so it is encrypted at rest and used only for the user's own review requests. For least privilege, use a GitHub App (read-only contents and pull requests, plus pull-request write only if publishing is wanted) and set `GITHUB_OAUTH_SCOPES=read:user` for public-repository-only use.

## Render deployment notes

- Enter secrets (`SESSION_SECRET`, `GITHUB_OAUTH_CLIENT_SECRET`, `GITHUB_TOKEN` or App key, `LLM_API_KEY`, `DEMO_ACCOUNTS`, webhook secret) in the Render dashboard only; `render.yaml` marks them `sync: false` (or generates them) and must never contain values.
- The free Render Postgres database expires after 30 days. Review history and sessions are lost with it, so treat the free tier as demonstration-only and export anything worth keeping.
- Set `SESSION_SECRET` explicitly; without it sessions do not survive a restart.

For stronger multi-tenant isolation, execute Ruff and Semgrep in a per-review sandbox with CPU, memory, process and network limits. The bundled Compose deployment is suitable for an academic demonstration, not an internet-facing shared SaaS without an external job sandbox and secret manager.
