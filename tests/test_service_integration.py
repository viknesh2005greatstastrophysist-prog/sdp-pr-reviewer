import asyncio

from pr_reviewer.database import ReviewStore
from pr_reviewer.models import PullRequestRef
from pr_reviewer.service import ReviewService
from pr_reviewer.settings import Settings

DEMO_DIFF = """diff --git a/app.py b/app.py
new file mode 100644
--- /dev/null
+++ b/app.py
@@ -0,0 +1,2 @@
+def evaluate(expression: str):
+    return eval(expression)
"""


class FakeGitHub:
    async def pull_request(self, ref):
        return {"head": {"sha": "abc123"}}

    async def diff(self, ref):
        return DEMO_DIFF

    async def materialize_changed_files(self, ref, files, destination):
        (destination / "app.py").write_text(
            "def evaluate(expression: str):\n    return eval(expression)\n",
            encoding="utf-8",
        )
        return []


def test_complete_service_path_persists_review(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'reviews.db'}"
    store = ReviewStore(database_url)
    store.create_schema()
    service = ReviewService(
        Settings(database_url=database_url),
        FakeGitHub(),
        store,
    )
    ref = PullRequestRef(owner="academic", repo="demo", number=1)
    result = asyncio.run(service.review_pull_request(ref))
    assert result.head_sha == "abc123"
    assert any("ruff" in item.source.split("+") for item in result.findings)
    assert store.history("academic/demo")[0]["finding_count"] >= 1
