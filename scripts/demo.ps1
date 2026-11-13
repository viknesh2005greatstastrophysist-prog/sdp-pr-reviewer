$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PATH = "$projectRoot\.venv\Scripts;$env:PATH"
& "$projectRoot\.venv\Scripts\pr-reviewer.exe" `
  "$projectRoot\examples\demo.diff" `
  --repo "$projectRoot\examples\demo_repo" `
  --offline
