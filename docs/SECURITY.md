# Security model

- Repository content, diffs, filenames and comments are untrusted data. Prompts state this boundary and structured outputs are revalidated against the added-line map.
- Changed contents are fetched through GitHub's contents API into a new temporary directory. Paths are resolved beneath that directory and files over 1 MB are skipped.
- Repository code is never imported, executed, tested or passed to a shell. AST parsing is syntax-only. Analyzer subprocesses use argument arrays with `shell=False`.
- Webhooks require an HMAC-SHA256 signature and a configured secret.
- Credentials come from environment variables. GitHub App private keys are short-lived JWT inputs; installation tokens are preferred over personal tokens.
- Suggestions are constrained to added lines, bounded in size, syntax-checked for Python and posted for human action only.
- Publishing is opt-in and always uses GitHub's `COMMENT` event. The application contains no approve or merge operation.
- The container runs as a non-root user, with a read-only filesystem, `no-new-privileges`, and a no-execute temporary filesystem in Compose.

For stronger multi-tenant isolation, execute Ruff and Semgrep in a per-review sandbox with CPU, memory, process and network limits. The bundled Compose deployment is suitable for an academic demonstration, not an internet-facing shared SaaS without an external job sandbox and secret manager.
