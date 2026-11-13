from fastapi.testclient import TestClient

from pr_reviewer.api import app


def test_health():
    response = TestClient(app).get("/health")
    assert response.status_code == 200


def test_dashboard_is_served():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "PRISM" in response.text
    assert "Demo" in response.text
    assert "Live" in response.text


def test_dashboard_status_does_not_expose_secrets():
    response = TestClient(app).get("/dashboard/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "ready"
    assert set(payload["agents"]) == {"bug-hunter", "performance", "style", "architecture"}
    assert "github_token" not in payload
    assert "llm_api_key" not in payload


def test_dashboard_demo_runs_pipeline():
    response = TestClient(app).post("/dashboard/demo")
    assert response.status_code == 200
    payload = response.json()
    assert payload["files"][0]["path"] == "app.py"
    assert len(payload["agent_runs"]) == 4
    assert any(item["rule_id"] == "S307" for item in payload["findings"])
    assert any(
        item.get("suggestion", {}).get("validated")
        for item in payload["findings"]
        if item.get("suggestion")
    )


def test_dashboard_lists_distinct_demo_cases():
    response = TestClient(app).get("/dashboard/demo-cases")
    assert response.status_code == 200
    cases = response.json()
    assert len(cases) >= 6
    assert len({item["case_id"] for item in cases}) == len(cases)
    assert {"security", "performance", "clean", "resilience"}.issubset(
        {item["case_id"] for item in cases}
    )


def test_clean_demo_abstains():
    response = TestClient(app).post("/dashboard/demo", json={"case_id": "clean"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["findings"] == []
    assert all(run["status"] == "success" for run in payload["agent_runs"])


def test_resilience_demo_isolates_agent_failure():
    response = TestClient(app).post("/dashboard/demo", json={"case_id": "resilience"})
    assert response.status_code == 200
    payload = response.json()
    statuses = {run["agent"]: run["status"] for run in payload["agent_runs"]}
    assert statuses["architecture"] == "failed"
    assert sum(status == "success" for status in statuses.values()) == 3
    assert any("architecture agent failed" in warning for warning in payload["warnings"])


def test_unknown_demo_case_is_rejected():
    response = TestClient(app).post("/dashboard/demo", json={"case_id": "not-a-case"})
    assert response.status_code == 404


def test_webhook_requires_configuration():
    response = TestClient(app).post(
        "/webhooks/github", content=b"{}", headers={"X-GitHub-Event": "pull_request"}
    )
    assert response.status_code == 503
