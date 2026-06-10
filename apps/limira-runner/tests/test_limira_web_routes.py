import io
import json
import os
import sys
import zipfile
import asyncio
import hashlib
import sqlite3
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import httpx
from fastapi import FastAPI, HTTPException


ROOT = Path(__file__).resolve().parents[3]
LIMIRA_BACKEND = ROOT / "apps/limira-web/backend"
sys.path.insert(0, str(LIMIRA_BACKEND))

from limira_backend.routers import limira  # noqa: E402


PLAN_NAMED_OBJECT_KEY_ALIASES = (
    "archive_object_key",
    "archiveObjectKey",
    "pdf_object_key",
    "pdfObjectKey",
)


def _latest_auth_link_token(outbox_path: Path, query_key: str) -> str:
    records = [json.loads(line) for line in outbox_path.read_text(encoding="utf-8").splitlines()]
    for record in reversed(records):
        for word in str(record.get("body") or "").split():
            parsed = urlsplit(word.strip())
            values = parse_qs(parsed.query).get(query_key)
            if values:
                return values[0]
    raise AssertionError(f"missing {query_key} token in auth email outbox")


class FakeArchiveClient:
    def __init__(self, archive_bytes: bytes | None = None):
        self.archive_bytes = archive_bytes or _archive_zip()
        self.calls = []

    async def download_archive(self, task, user):
        self.calls.append({"task": task, "user": user})
        return self.archive_bytes


class FakeResearchClient:
    def __init__(
        self,
        *,
        runner_task_id="runner-task-a",
        events=None,
        status_payload=None,
        stream_exception=None,
    ):
        self.runner_task_id = runner_task_id
        self.events = events or []
        self.status_payload = status_payload or {
            "task_id": runner_task_id,
            "status": "completed",
            "archive_status": "ready",
        }
        self.stream_exception = stream_exception
        self.create_calls = []
        self.stream_calls = []
        self.status_calls = []

    async def create_research_task(self, *, query, scenario, user):
        self.create_calls.append(
            {
                "query": query,
                "scenario": scenario,
                "user": user,
            }
        )
        return {
            "task_id": self.runner_task_id,
            "status": "queued",
            "stream_url": f"/limira-runner/tasks/{self.runner_task_id}/events",
            "task_url": f"/limira-runner/tasks/{self.runner_task_id}",
        }

    async def stream_events(self, *, task, user):
        self.stream_calls.append({"task": task, "user": user})
        if self.stream_exception:
            raise self.stream_exception
        for event in self.events:
            yield event

    async def get_task_status(self, *, task, user):
        self.status_calls.append({"task": task, "user": user})
        return dict(self.status_payload)


@pytest.mark.asyncio
async def test_create_research_uses_limira_namespace_and_rejects_body_user_id():
    repo = limira.InMemoryLimiraTaskRepository()
    research = FakeResearchClient()
    user = limira.LimiraUser("user-a")

    payload = await limira.create_research_task(
        {"query": "track sanctions", "scenario": "sanctions"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )

    assert payload["task_url"].startswith("/api/limira/tasks/")
    assert payload["events_url"].startswith("/api/limira/tasks/")
    assert payload["artifacts_url"].startswith("/api/limira/tasks/")
    assert payload["scenario"] == "sanctions"
    assert payload["query"] == "track sanctions"
    assert research.create_calls == [
        {
            "query": "track sanctions",
            "scenario": "sanctions",
            "user": user,
        }
    ]
    task = repo.get_task(payload["task_id"])
    assert task.runner_task_id == "runner-task-a"
    _assert_no_browser_leak(payload)

    with pytest.raises(HTTPException) as rejected:
        await limira.create_research_task(
            {"query": "track sanctions", "user_id": "attacker"},
            request=None,
            user=user,
            repo=repo,
            research_client=research,
        )
    assert rejected.value.status_code == 400


@pytest.mark.asyncio
async def test_create_research_rejects_body_user_spoofing_on_actual_http_surface():
    app, repo, _storage = _limira_asgi_app()
    research = FakeResearchClient()

    async def research_client_override():
        return research

    app.dependency_overrides[limira.get_research_client] = research_client_override

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        for forbidden_field in ("user_id", "owner_user_id"):
            response = await client.post(
                "/api/limira/research",
                json={
                    "query": "attempt request body identity spoofing",
                    forbidden_field: "attacker",
                },
            )

            assert response.status_code == 400
            assert response.json() == {"detail": "user_id_not_allowed"}

    assert repo.tasks == {}
    assert research.create_calls == []


@pytest.mark.asyncio
async def test_limira_native_auth_drives_browser_facing_api_without_legacy_auth_proxy(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")
    outbox_path = tmp_path / "auth-outbox.jsonl"
    monkeypatch.setenv(limira.LIMIRA_AUTH_EMAIL_OUTBOX_PATH_ENV, str(outbox_path))

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        unauthenticated = await client.get("/api/limira/scenarios")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json() == {"detail": "not_authenticated"}

        signup = await client.post(
            "/api/limira/auth/signup",
            json={
                "email": "analyst@example.test",
                "name": "Analyst",
                "password": "correct-password",
            },
        )
        assert signup.status_code == 201
        signup_payload = signup.json()
        assert signup_payload["email"] == "analyst@example.test"
        assert signup_payload["name"] == "Analyst"
        assert signup_payload["role"] == "user"
        assert signup_payload["email_verified"] is False
        assert signup_payload["email_verification_required"] is True
        assert signup_payload["email_delivery"] == "outbox"
        assert "token" not in signup_payload
        assert "limira_session=" not in signup.headers.get("set-cookie", "")

        unverified_signin = await client.post(
            "/api/limira/auth/signin",
            json={"email": "analyst@example.test", "password": "correct-password"},
        )
        assert unverified_signin.status_code == 403
        assert unverified_signin.json() == {"detail": "email_not_verified"}

        verify_token = _latest_auth_link_token(outbox_path, "verify_email_token")
        verified = await client.post(
            "/api/limira/auth/verify-email",
            json={"token": verify_token},
        )
        assert verified.status_code == 200
        verified_payload = verified.json()
        assert verified_payload["email"] == "analyst@example.test"
        assert verified_payload["email_verified"] is True
        assert verified_payload["token_type"] == "bearer"
        token = verified_payload["token"]
        assert token
        assert "limira_session=" in verified.headers.get("set-cookie", "")

        session = await client.get(
            "/api/limira/auth/session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert session.status_code == 200
        assert session.json() == {
            "id": signup_payload["id"],
            "email": "analyst@example.test",
            "name": "Analyst",
            "role": "user",
            "email_verified": True,
            "account_type": "personal",
            "organization_id": None,
            "organization_role": None,
            "daily_research_limit": 1,
        }

        scenarios = await client.get(
            "/api/limira/scenarios",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert scenarios.status_code == 200
        assert scenarios.json()["count"] == 3

        rejected = await client.post(
            "/api/limira/auth/signin",
            json={"email": "analyst@example.test", "password": "wrong-password"},
        )
        assert rejected.status_code == 401
        assert rejected.json() == {"detail": "invalid_credentials"}

        signin = await client.post(
            "/api/limira/auth/signin",
            json={"email": "analyst@example.test", "password": "correct-password"},
        )
        assert signin.status_code == 200
        assert signin.json()["email"] == "analyst@example.test"
        assert signin.json()["email_verified"] is True


@pytest.mark.asyncio
async def test_limira_native_auth_password_reset_uses_email_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")
    outbox_path = tmp_path / "auth-outbox.jsonl"
    monkeypatch.setenv(limira.LIMIRA_AUTH_EMAIL_OUTBOX_PATH_ENV, str(outbox_path))

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        signup = await client.post(
            "/api/limira/auth/signup",
            json={
                "email": "reset@example.test",
                "name": "Reset User",
                "password": "old-password",
            },
        )
        assert signup.status_code == 201

        verify_token = _latest_auth_link_token(outbox_path, "verify_email_token")
        verified = await client.post("/api/limira/auth/verify-email", json={"token": verify_token})
        assert verified.status_code == 200

        requested = await client.post(
            "/api/limira/auth/password-reset/request",
            json={"email": "reset@example.test"},
        )
        assert requested.status_code == 200
        assert requested.json() == {"ok": True}

        reset_token = _latest_auth_link_token(outbox_path, "reset_password_token")
        reset = await client.post(
            "/api/limira/auth/password-reset/confirm",
            json={"token": reset_token, "password": "new-password"},
        )
        assert reset.status_code == 200
        reset_payload = reset.json()
        assert reset_payload["email"] == "reset@example.test"
        assert reset_payload["email_verified"] is True
        assert reset_payload["token"]
        assert "limira_session=" in reset.headers.get("set-cookie", "")

        reused = await client.post(
            "/api/limira/auth/password-reset/confirm",
            json={"token": reset_token, "password": "another-password"},
        )
        assert reused.status_code == 400
        assert reused.json() == {"detail": "invalid_or_expired_auth_token"}

        old_password = await client.post(
            "/api/limira/auth/signin",
            json={"email": "reset@example.test", "password": "old-password"},
        )
        assert old_password.status_code == 401

        new_password = await client.post(
            "/api/limira/auth/signin",
            json={"email": "reset@example.test", "password": "new-password"},
        )
        assert new_password.status_code == 200
        assert new_password.json()["email"] == "reset@example.test"


@pytest.mark.asyncio
async def test_limira_enterprise_auth_lists_units_and_allows_org_admin_member_management(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")

    organization = limira._limira_auth_create_organization(
        name="Example Intelligence Unit",
        slug="example-unit",
    )
    admin = limira._limira_auth_insert_user(
        email="unit-admin@example.test",
        password="enterprise-password",
        name="Unit Admin",
        account_type="enterprise",
        organization_id=organization.id,
        organization_role="admin",
        email_verified_at=1,
    )

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        organizations = await client.get("/api/limira/auth/organizations")
        assert organizations.status_code == 200
        assert organizations.json()["organizations"] == [
            {
                "id": organization.id,
                "name": "Example Intelligence Unit",
                "slug": "example-unit",
                "billing_mode": "metered",
            }
        ]

        personal_signin = await client.post(
            "/api/limira/auth/signin",
            json={
                "email": "unit-admin@example.test",
                "password": "enterprise-password",
            },
        )
        assert personal_signin.status_code == 403
        assert personal_signin.json() == {"detail": "enterprise_login_required"}

        signin = await client.post(
            "/api/limira/auth/enterprise/signin",
            json={
                "organization_id": organization.id,
                "email": "unit-admin@example.test",
                "password": "enterprise-password",
            },
        )
        assert signin.status_code == 200
        payload = signin.json()
        assert payload["id"] == admin.id
        assert payload["account_type"] == "enterprise"
        assert payload["organization_id"] == organization.id
        assert payload["organization_role"] == "admin"
        assert payload["organization"]["billing_mode"] == "metered"
        token = payload["token"]

        members = await client.get(
            "/api/limira/enterprise/members",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert members.status_code == 200
        assert members.json()["count"] == 1
        assert members.json()["members"][0]["email"] == "unit-admin@example.test"

        created = await client.post(
            "/api/limira/enterprise/members",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "analyst-unit@example.test",
                "name": "Unit Analyst",
                "password": "member-password",
                "organization_role": "member",
            },
        )
        assert created.status_code == 201
        assert created.json()["member"]["account_type"] == "enterprise"
        assert created.json()["member"]["organization_id"] == organization.id
        assert created.json()["member"]["organization_role"] == "member"

        usage = await client.get(
            "/api/limira/enterprise/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert usage.status_code == 200
        assert usage.json()["organization"]["id"] == organization.id
        assert usage.json()["usage"]["totals"] == {}


@pytest.mark.asyncio
async def test_personal_auth_research_is_limited_to_one_task_per_utc_day(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")
    outbox_path = tmp_path / "auth-outbox.jsonl"
    monkeypatch.setenv(limira.LIMIRA_AUTH_EMAIL_OUTBOX_PATH_ENV, str(outbox_path))

    repo = limira.InMemoryLimiraTaskRepository()
    research = FakeResearchClient()
    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async def task_repository_override():
        return repo

    async def research_client_override():
        return research

    app.dependency_overrides[limira.get_task_repository] = task_repository_override
    app.dependency_overrides[limira.get_research_client] = research_client_override

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        signup = await client.post(
            "/api/limira/auth/signup",
            json={
                "email": "quota@example.test",
                "name": "Quota User",
                "password": "quota-password",
            },
        )
        assert signup.status_code == 201
        verify_token = _latest_auth_link_token(outbox_path, "verify_email_token")
        verified = await client.post(
            "/api/limira/auth/verify-email",
            json={"token": verify_token},
        )
        assert verified.status_code == 200
        token = verified.json()["token"]

        first = await client.post(
            "/api/limira/research",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "first personal quota task"},
        )
        assert first.status_code == 202

        second = await client.post(
            "/api/limira/research",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "second personal quota task"},
        )
        assert second.status_code == 429
        assert second.json() == {"detail": "personal_daily_quota_exceeded"}

    assert len(research.create_calls) == 1
    with sqlite3.connect(tmp_path / "limira_auth.sqlite3") as connection:
        usage_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM limira_auth_usage_events
            WHERE account_type = 'personal'
                AND event_type = 'research_task'
            """
        ).fetchone()[0]
    assert usage_count == 1


@pytest.mark.asyncio
async def test_enterprise_auth_research_records_metered_usage_without_daily_limit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")

    organization = limira._limira_auth_create_organization(
        name="Metered Research Unit",
        slug="metered-unit",
    )
    limira._limira_auth_insert_user(
        email="metered@example.test",
        password="metered-password",
        name="Metered User",
        account_type="enterprise",
        organization_id=organization.id,
        organization_role="member",
        email_verified_at=1,
    )

    repo = limira.InMemoryLimiraTaskRepository()
    research = FakeResearchClient()
    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async def task_repository_override():
        return repo

    async def research_client_override():
        return research

    app.dependency_overrides[limira.get_task_repository] = task_repository_override
    app.dependency_overrides[limira.get_research_client] = research_client_override

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        signin = await client.post(
            "/api/limira/auth/enterprise/signin",
            json={
                "organization_id": organization.id,
                "email": "metered@example.test",
                "password": "metered-password",
            },
        )
        assert signin.status_code == 200
        token = signin.json()["token"]

        for query in ("first enterprise task", "second enterprise task"):
            response = await client.post(
                "/api/limira/research",
                headers={"Authorization": f"Bearer {token}"},
                json={"query": query},
            )
            assert response.status_code == 202

    assert len(research.create_calls) == 2
    with sqlite3.connect(tmp_path / "limira_auth.sqlite3") as connection:
        usage = connection.execute(
            """
            SELECT organization_id, account_type, event_type, COUNT(*), SUM(quantity)
            FROM limira_auth_usage_events
            GROUP BY organization_id, account_type, event_type
            """
        ).fetchone()
    assert usage == (
        organization.id,
        "enterprise",
        "research_task",
        2,
        2,
    )


@pytest.mark.asyncio
async def test_limira_auth_email_links_use_forwarded_public_origin(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")
    outbox_path = tmp_path / "auth-outbox.jsonl"
    monkeypatch.setenv(limira.LIMIRA_AUTH_EMAIL_OUTBOX_PATH_ENV, str(outbox_path))

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://internal-backend.test",
    ) as client:
        signup = await client.post(
            "/api/limira/auth/signup",
            headers={
                "x-forwarded-host": "limira-public.example",
                "x-forwarded-proto": "https",
            },
            json={
                "email": "forwarded@example.test",
                "name": "Forwarded User",
                "password": "correct-password",
            },
        )
        assert signup.status_code == 201

    outbox = [json.loads(line) for line in outbox_path.read_text(encoding="utf-8").splitlines()]
    body = outbox[-1]["body"]
    assert "https://limira-public.example/limira?verify_email_token=" in body
    assert "internal-backend.test" not in body
    assert "127.0.0.1" not in body


@pytest.mark.asyncio
async def test_limira_google_oauth_config_and_start_are_env_gated(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")
    monkeypatch.delenv(limira.LIMIRA_GOOGLE_OAUTH_CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(limira.LIMIRA_GOOGLE_OAUTH_CLIENT_SECRET_ENV, raising=False)

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        disabled = await client.get("/api/limira/auth/google/config")
        assert disabled.status_code == 200
        assert disabled.json() == {"enabled": False}

        disabled_start = await client.get("/api/limira/auth/google/start")
        assert disabled_start.status_code == 404
        assert disabled_start.json() == {"detail": "google_oauth_not_configured"}

        monkeypatch.setenv(limira.LIMIRA_GOOGLE_OAUTH_CLIENT_ID_ENV, "google-client-id")
        monkeypatch.setenv(limira.LIMIRA_GOOGLE_OAUTH_CLIENT_SECRET_ENV, "google-secret")
        monkeypatch.setenv(limira.LIMIRA_GOOGLE_OAUTH_AUTH_URL_ENV, "https://google.example/auth")

        enabled = await client.get("/api/limira/auth/google/config")
        assert enabled.status_code == 200
        assert enabled.json() == {"enabled": True}

        start = await client.get("/api/limira/auth/google/start", follow_redirects=False)
        assert start.status_code == 303
        location = start.headers["location"]
        parsed = urlsplit(location)
        params = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://google.example/auth"
        assert params["client_id"] == ["google-client-id"]
        assert params["response_type"] == ["code"]
        assert params["scope"] == ["openid email profile"]
        assert params["redirect_uri"] == ["http://limira.test/api/limira/auth/google/callback"]
        assert params["state"][0]
        assert limira.LIMIRA_GOOGLE_OAUTH_STATE_COOKIE_NAME in start.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_limira_google_oauth_callback_creates_verified_session_user(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")
    monkeypatch.setenv(limira.LIMIRA_GOOGLE_OAUTH_CLIENT_ID_ENV, "google-client-id")
    monkeypatch.setenv(limira.LIMIRA_GOOGLE_OAUTH_CLIENT_SECRET_ENV, "google-secret")

    async def fake_google_userinfo_from_code(*, code, request, env=os.environ):
        assert code == "google-code"
        return {
            "google_sub": "google-user-123",
            "email": "google@example.test",
            "name": "Google User",
        }

    monkeypatch.setattr(
        limira,
        "_limira_google_userinfo_from_code",
        fake_google_userinfo_from_code,
    )

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        start = await client.get("/api/limira/auth/google/start", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

        callback = await client.get(
            "/api/limira/auth/google/callback",
            params={"code": "google-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "http://limira.test/limira?google_auth=success"
        assert "limira_session=" in callback.headers.get("set-cookie", "")

        session = await client.get("/api/limira/auth/session")
        assert session.status_code == 200
        assert session.json()["email"] == "google@example.test"
        assert session.json()["name"] == "Google User"
        assert session.json()["role"] == "user"
        assert session.json()["email_verified"] is True


@pytest.mark.asyncio
async def test_limira_wechat_oauth_config_and_start_are_env_gated(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")
    monkeypatch.delenv(limira.LIMIRA_WECHAT_OAUTH_APP_ID_ENV, raising=False)
    monkeypatch.delenv(limira.LIMIRA_WECHAT_OAUTH_APP_SECRET_ENV, raising=False)

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        disabled = await client.get("/api/limira/auth/wechat/config")
        assert disabled.status_code == 200
        assert disabled.json() == {"enabled": False}

        disabled_start = await client.get("/api/limira/auth/wechat/start")
        assert disabled_start.status_code == 404
        assert disabled_start.json() == {"detail": "wechat_oauth_not_configured"}

        monkeypatch.setenv(limira.LIMIRA_WECHAT_OAUTH_APP_ID_ENV, "wechat-app-id")
        monkeypatch.setenv(limira.LIMIRA_WECHAT_OAUTH_APP_SECRET_ENV, "wechat-secret")
        monkeypatch.setenv(limira.LIMIRA_WECHAT_OAUTH_AUTH_URL_ENV, "https://wechat.example/connect")

        enabled = await client.get("/api/limira/auth/wechat/config")
        assert enabled.status_code == 200
        assert enabled.json() == {"enabled": True}

        start = await client.get("/api/limira/auth/wechat/start", follow_redirects=False)
        assert start.status_code == 303
        location = start.headers["location"]
        parsed = urlsplit(location)
        params = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://wechat.example/connect"
        assert parsed.fragment == "wechat_redirect"
        assert params["appid"] == ["wechat-app-id"]
        assert params["response_type"] == ["code"]
        assert params["scope"] == ["snsapi_login"]
        assert params["redirect_uri"] == ["http://limira.test/api/limira/auth/wechat/callback"]
        assert params["state"][0]
        assert limira.LIMIRA_WECHAT_OAUTH_STATE_COOKIE_NAME in start.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_limira_wechat_oauth_callback_creates_verified_session_user(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(tmp_path / "limira_auth.sqlite3"))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(tmp_path / "missing.db"))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")
    monkeypatch.setenv(limira.LIMIRA_WECHAT_OAUTH_APP_ID_ENV, "wechat-app-id")
    monkeypatch.setenv(limira.LIMIRA_WECHAT_OAUTH_APP_SECRET_ENV, "wechat-secret")

    async def fake_wechat_userinfo_from_code(*, code, env=os.environ):
        assert code == "wechat-code"
        return {
            "wechat_sub": "wechat-unionid-123",
            "openid": "wechat-openid-123",
            "unionid": "wechat-unionid-123",
            "name": "微信测试用户",
        }

    monkeypatch.setattr(
        limira,
        "_limira_wechat_userinfo_from_code",
        fake_wechat_userinfo_from_code,
    )

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        start = await client.get("/api/limira/auth/wechat/start", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

        callback = await client.get(
            "/api/limira/auth/wechat/callback",
            params={"code": "wechat-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "http://limira.test/limira?wechat_auth=success"
        assert "limira_session=" in callback.headers.get("set-cookie", "")

        session = await client.get("/api/limira/auth/session")
        assert session.status_code == 200
        session_payload = session.json()
        assert session_payload["email"].endswith("@auth.limira.local")
        assert session_payload["name"] == "微信测试用户"
        assert session_payload["role"] == "user"
        assert session_payload["email_verified"] is True

        second_start = await client.get("/api/limira/auth/wechat/start", follow_redirects=False)
        second_state = parse_qs(urlsplit(second_start.headers["location"]).query)["state"][0]
        second_callback = await client.get(
            "/api/limira/auth/wechat/callback",
            params={"code": "wechat-code", "state": second_state},
            follow_redirects=False,
        )
        assert second_callback.status_code == 303
        assert "limira_session=" in second_callback.headers.get("set-cookie", "")

        with sqlite3.connect(tmp_path / "limira_auth.sqlite3") as connection:
            user_count = connection.execute("SELECT COUNT(*) FROM limira_auth_users").fetchone()[0]
            identity_count = connection.execute("SELECT COUNT(*) FROM limira_auth_identities").fetchone()[0]
        assert user_count == 1
        assert identity_count == 1


@pytest.mark.asyncio
async def test_limira_native_auth_migrates_existing_legacy_sqlite_user_once(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "limira_auth.sqlite3"
    legacy_path = tmp_path / "legacy_auth.sqlite3"
    password_hash = limira._limira_hash_password("existing-password")
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "CREATE TABLE auth (id TEXT PRIMARY KEY, email TEXT, password TEXT, active INTEGER)"
        )
        connection.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, name TEXT, role TEXT)')
        connection.execute(
            "INSERT INTO auth (id, email, password, active) VALUES (?, ?, ?, 1)",
            ("existing-user", "existing@example.test", password_hash),
        )
        connection.execute(
            'INSERT INTO "user" (id, email, name, role) VALUES (?, ?, ?, ?)',
            ("existing-user", "existing@example.test", "Existing User", "admin"),
        )

    monkeypatch.setenv(limira.LIMIRA_AUTH_SQLITE_PATH_ENV, str(auth_path))
    monkeypatch.setenv(limira.LIMIRA_LEGACY_AUTH_SQLITE_PATH_ENV, str(legacy_path))
    monkeypatch.setenv(limira.LIMIRA_AUTH_SECRET_ENV, "test-limira-auth-secret")

    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        signin = await client.post(
            "/api/limira/auth/signin",
            json={"email": "existing@example.test", "password": "existing-password"},
        )
        assert signin.status_code == 200
        assert signin.json()["id"] == "existing-user"
        assert signin.json()["name"] == "Existing User"
        assert signin.json()["role"] == "admin"

    assert auth_path.exists()
    with sqlite3.connect(auth_path) as connection:
        migrated = connection.execute(
            "SELECT email, name, role FROM limira_auth_users WHERE id = ?",
            ("existing-user",),
        ).fetchone()
    assert migrated == ("existing@example.test", "Existing User", "admin")


@pytest.mark.asyncio
async def test_demo_scenarios_are_browser_safe_and_artifact_oriented():
    payload = await limira.list_demo_scenarios(user=limira.LimiraUser("user-a"))

    scenario_ids = {scenario["id"] for scenario in payload["scenarios"]}
    assert scenario_ids == {
        "sanctions_export_controls",
        "geopolitical_risk_assessment",
        "critical_minerals_competition",
    }
    assert payload["count"] == 3
    _assert_no_browser_leak(payload)

    payload_text = json.dumps(payload)
    assert "Artifact requirements" not in payload_text
    assert "record_research_artifact" not in payload_text
    assert "runner" not in payload_text.lower()

    for scenario_id in scenario_ids:
        runner_query = limira._runner_query_for_scenario("base query", scenario_id)
        assert "base query" in runner_query
        assert "record_research_artifact" in runner_query
        assert "EVID-001" in runner_query
        assert "map_feature" in runner_query
        assert "[EVID-001]" in runner_query
        assert "report_section" in runner_query

    assert limira._runner_query_for_scenario("base query", "legacy-scenario") == "base query"


@pytest.mark.asyncio
async def test_create_research_with_known_demo_scenario_enriches_runner_query_only():
    repo = limira.InMemoryLimiraTaskRepository()
    research = FakeResearchClient()
    user = limira.LimiraUser("user-a")
    scenario_id = "critical_minerals_competition"

    payload = await limira.create_research_task(
        {"query": "Analyze nickel supply chain risk", "scenario": scenario_id},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )

    assert payload["scenario"] == scenario_id
    assert payload["query"] == "Analyze nickel supply chain risk"
    _assert_no_browser_leak(payload)
    assert "record_research_artifact" not in json.dumps(payload)

    assert len(research.create_calls) == 1
    runner_call = research.create_calls[0]
    assert runner_call["scenario"] == scenario_id
    assert runner_call["user"] == user
    assert runner_call["query"] != payload["query"]
    assert "Critical minerals competition" in runner_call["query"]
    assert "Analyze nickel supply chain risk" in runner_call["query"]
    assert "record_research_artifact" in runner_call["query"]
    assert "EVID-001" in runner_call["query"]
    assert "map_feature" in runner_call["query"]
    assert "[EVID-001]" in runner_call["query"]

    task = repo.get_task(payload["task_id"])
    assert task.query == "Analyze nickel supply chain risk"
    assert task.scenario == scenario_id


@pytest.mark.asyncio
async def test_create_research_records_failed_web_task_when_runner_start_fails():
    repo = limira.InMemoryLimiraTaskRepository()
    research = FakeResearchClient()
    user = limira.LimiraUser("user-a")

    async def fail_create_research_task(*_args, **_kwargs):
        raise HTTPException(status_code=503, detail="runner_unavailable")

    research.create_research_task = fail_create_research_task

    with pytest.raises(HTTPException) as failed:
        await limira.create_research_task(
            {"query": "runner fails after web task is allocated"},
            request=None,
            user=user,
            repo=repo,
            research_client=research,
        )

    assert failed.value.status_code == 503
    assert len(repo.tasks) == 1
    task = next(iter(repo.tasks.values()))
    assert task.owner_user_id == "user-a"
    assert task.status == "failed"
    assert task.archive_status == "failed"
    assert task.error == "runner_unavailable"
    assert task.runner_task_id is None


@pytest.mark.asyncio
async def test_create_research_hides_internal_runner_start_error_details_and_headers_from_browser_and_task_state():
    app, repo, _storage = _limira_asgi_app()
    internal_detail = (
        "runner create failed at http://10.20.30.40:8091/limira-runner/research "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt "
        "OPENAI_API_KEY=sk-researchstartinternal123456"
    )

    class FailingResearchClient(FakeResearchClient):
        async def create_research_task(self, *, query, scenario, user):
            self.create_calls.append(
                {
                    "query": query,
                    "scenario": scenario,
                    "user": user,
                }
            )
            raise HTTPException(
                status_code=503,
                detail=internal_detail,
                headers={
                    "X-Limira-Runner-Service-Token": "server-only-token-123",
                    "Authorization": "Bearer research-start-token-123",
                    "Set-Cookie": "limira_session=research-start-cookie-123",
                    "X-API-Key": "sk-researchheader123456",
                },
            )

    research = FailingResearchClient()

    async def research_client_override():
        return research

    app.dependency_overrides[limira.get_research_client] = research_client_override

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/research",
            json={"query": "runner start internal detail"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "runner_research_start_failed"
    assert len(repo.tasks) == 1
    task = next(iter(repo.tasks.values()))
    assert task.owner_user_id == "user-a"
    assert task.status == "failed"
    assert task.archive_status == "failed"
    assert task.error == "runner_research_start_failed"
    assert task.runner_task_id is None
    for header in (
        "x-limira-runner-service-token",
        "authorization",
        "set-cookie",
        "x-api-key",
    ):
        assert header not in response.headers
    serialized = json.dumps(
        {
            "response": response.json(),
            "headers": dict(response.headers),
            "task": task.public_dict(),
            "repo_error": task.error,
        },
        ensure_ascii=False,
    )
    for leaked in (
        "http://10.20.30.40:8091",
        "limira/users/hash",
        "sk-researchstartinternal123456",
        "server-only-token-123",
        "research-start-token-123",
        "research-start-cookie-123",
        "sk-researchheader123456",
        "OPENAI_API_KEY",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(response.json())
    _assert_no_browser_leak(task.public_dict())


@pytest.mark.asyncio
async def test_user_isolation_for_task_status_and_archive_download():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    research = FakeResearchClient()
    user_a = limira.LimiraUser("user-a")
    user_b = limira.LimiraUser("user-b")

    created = await limira.create_research_task(
        {"query": "red sea shipping risk"},
        request=None,
        user=user_a,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.tasks[task_id].archive_status = "ready"
    repo.tasks[task_id].runner_task_id = "runner-task-a"

    with pytest.raises(HTTPException) as forbidden_status:
        await limira.get_task(task_id, user=user_b, repo=repo)
    assert forbidden_status.value.status_code == 404

    with pytest.raises(HTTPException) as forbidden_archive:
        await limira.download_task_archive(
            task_id,
            user=user_b,
            repo=repo,
            object_storage=storage,
        )
    assert forbidden_archive.value.status_code == 404

    response = await limira.download_task_archive(
        task_id,
        user=user_a,
        repo=repo,
        object_storage=storage,
    )

    assert response.media_type == "application/zip"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert zipfile.ZipFile(io.BytesIO(response.body)).namelist()[:4] == list(
        limira.ARCHIVE_MEMBER_ORDER
    )
    archive_key = repo.tasks[task_id].archive_object_key
    assert archive_key in storage.objects
    assert "/tasks/" in archive_key
    assert "/archives/" in archive_key
    assert (
        repo.tasks[task_id].archive_zip_sha256
        == storage.objects[archive_key]["sha256"]
    )
    assert storage.objects[archive_key]["content_type"] == "application/zip"
    assert storage.objects[archive_key]["metadata"]["task_id"] == task_id
    assert storage.objects[archive_key]["metadata"]["owner_user_id"] == "user-a"
    _assert_public_archive_hides_internal_identifiers(response.body)
    second_response = await limira.download_task_archive(
        task_id,
        user=user_a,
        repo=repo,
        object_storage=storage,
    )
    assert second_response.body == response.body
    assert len(storage.objects) == 1
    _assert_no_browser_leak(response.body.decode("latin1"))


@pytest.mark.asyncio
async def test_archive_download_regenerates_mismatched_persisted_archive_object():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-archive-mismatch",
        owner_user_id=user.id,
        query="archive mismatch",
        scenario=None,
        runner_task_id="runner-archive-mismatch",
    )
    task.archive_status = "ready"

    first_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    archive_key = task.archive_object_key
    assert archive_key in storage.objects
    first_sha = task.archive_zip_sha256
    assert first_sha == hashlib.sha256(first_response.body).hexdigest()

    tampered_archive = _archive_zip()
    tampered_sha = hashlib.sha256(tampered_archive).hexdigest()
    assert tampered_sha != first_sha
    storage.objects[archive_key]["data"] = tampered_archive
    storage.objects[archive_key]["sha256"] = tampered_sha

    second_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert second_response.body != tampered_archive
    assert task.archive_object_key == archive_key
    assert task.archive_zip_sha256 == hashlib.sha256(second_response.body).hexdigest()
    assert storage.objects[archive_key]["sha256"] == task.archive_zip_sha256
    members = _archive_member_texts(second_response.body)
    metadata = json.loads(members["metadata.json"])
    assert metadata["task"]["task_id"] == task.task_id
    assert "archive mismatch" in members["report.md"]


@pytest.mark.asyncio
async def test_archive_download_regenerates_invalid_persisted_archive_object():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-archive-invalid-object",
        owner_user_id=user.id,
        query="archive invalid object",
        scenario=None,
        runner_task_id="runner-archive-invalid-object",
    )
    task.archive_status = "ready"

    first_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    archive_key = task.archive_object_key
    assert archive_key in storage.objects
    corrupt_archive = b"not a zip archive"
    corrupt_sha = hashlib.sha256(corrupt_archive).hexdigest()
    assert corrupt_sha != hashlib.sha256(first_response.body).hexdigest()
    storage.objects[archive_key]["data"] = corrupt_archive
    storage.objects[archive_key]["sha256"] = corrupt_sha

    second_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert second_response.body != corrupt_archive
    assert task.archive_object_key == archive_key
    assert task.archive_zip_sha256 == hashlib.sha256(second_response.body).hexdigest()
    assert zipfile.ZipFile(io.BytesIO(second_response.body)).namelist()[:4] == list(
        limira.ARCHIVE_MEMBER_ORDER
    )


@pytest.mark.asyncio
async def test_archive_download_regenerates_invalid_persisted_archive_object_key():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-archive-invalid-key",
        owner_user_id=user.id,
        query="archive invalid key",
        scenario=None,
        runner_task_id="runner-archive-invalid-key",
    )
    task.archive_status = "ready"
    task.archive_object_key = "../bad.zip"
    task.archive_zip_sha256 = "0" * 64

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert task.archive_object_key != "../bad.zip"
    assert task.archive_object_key in storage.objects
    assert ".." not in task.archive_object_key.split("/")
    assert "/tasks/task-archive-invalid-key/archives/" in task.archive_object_key
    assert task.archive_zip_sha256 == hashlib.sha256(response.body).hexdigest()
    assert storage.objects[task.archive_object_key]["sha256"] == task.archive_zip_sha256
    assert zipfile.ZipFile(io.BytesIO(response.body)).namelist()[:4] == list(
        limira.ARCHIVE_MEMBER_ORDER
    )


@pytest.mark.asyncio
async def test_postgres_archive_download_regenerates_invalid_persisted_archive_object_key():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task_id = "task-postgres-archive-invalid-key"
    repo.create_task(
        task_id=task_id,
        owner_user_id=user.id,
        query="postgres archive invalid key",
        scenario=None,
        runner_task_id="runner-postgres-archive-invalid-key",
    )
    repo.update_task(
        task_id,
        archive_status="ready",
        archive_object_key="../bad.zip",
        archive_zip_sha256="0" * 64,
    )

    response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    persisted = engine.tasks[task_id]
    assert persisted["archive_object_key"] != "../bad.zip"
    assert persisted["archive_object_key"] in storage.objects
    assert ".." not in persisted["archive_object_key"].split("/")
    assert f"/tasks/{task_id}/archives/" in persisted["archive_object_key"]
    assert persisted["archive_zip_sha256"] == hashlib.sha256(response.body).hexdigest()
    assert storage.objects[persisted["archive_object_key"]]["sha256"] == persisted["archive_zip_sha256"]
    metadata = json.loads(_archive_member_texts(response.body)["metadata.json"])
    assert metadata["task"]["task_id"] == task_id


@pytest.mark.asyncio
async def test_archive_proxy_scrubs_allowed_text_members_before_download():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-secret-archive",
        owner_user_id=user.id,
        query="archive secrets",
        scenario=None,
        runner_task_id="runner-secret-archive",
    )
    task.archive_status = "ready"
    repo.record_artifact(
        task.task_id,
        "evidence",
        {
            "evidence_id": "EVID-001",
            "summary": "Authorization: Bearer runner-token-123456",
            "url": "https://search.test?q=x&token=archive-token-123456",
        },
    )
    repo.record_generated_report(
        report_id="report-secret",
        task_id=task.task_id,
        report_type="final",
        markdown=(
            "# report\n"
            "OPENAI_API_KEY=sk-archiveopenai123456\n"
            "RUNNER_SERVICE_TOKEN=archive-runner-token-123456\n"
            "https://api.test/resource?api_key=archive-query-secret-123456"
        ),
        html=None,
        pdf_object_key=None,
        evidence_refs=["EVID-001"],
        creator_user_id=user.id,
        metadata={
            "cookie": "legacy_session=session-secret-123456",
            "deepseek": "DEEPSEEK_API_KEY=sk-tracedeepseek123456",
        },
    )

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.body)) as scrubbed_archive:
        assert scrubbed_archive.namelist()[:4] == list(limira.ARCHIVE_MEMBER_ORDER)
        combined = "\n".join(
            scrubbed_archive.read(member).decode("utf-8")
            for member in scrubbed_archive.namelist()
        )
        metadata = json.loads(scrubbed_archive.read("metadata.json"))
        trace = json.loads(scrubbed_archive.read("trace.json"))

    assert limira.LIMIRA_SECRET_REDACTION in combined
    _assert_no_raw_secret(combined)
    assert metadata["reports"][0]["report_id"] == "report-secret"
    assert trace["artifacts"]["evidence"][0]["evidence_id"] == "EVID-001"
    assert task.archive_object_key in storage.objects
    _assert_no_raw_secret(storage.objects[task.archive_object_key]["metadata"])


@pytest.mark.asyncio
async def test_archive_download_hides_internal_model_summary_identifiers():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-summary-archive",
        owner_user_id=user.id,
        query="archive internal state",
        scenario=None,
        runner_task_id="runner-task-secret",
    )
    repo.update_task(
        task.task_id,
        archive_status="ready",
        model_summary=_internal_model_summary(task.task_id),
    )

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    _assert_archive_hides_internal_model_summary_identifiers(response.body)
    assert task.archive_object_key in storage.objects


@pytest.mark.asyncio
async def test_archive_download_repairs_reused_persisted_model_summary_identifiers():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-summary-archive-reuse",
        owner_user_id=user.id,
        query="archive internal state reuse",
        scenario=None,
        runner_task_id="runner-task-secret",
    )
    raw_archive = _archive_zip_with_raw_model_summary(
        task_id=task.task_id,
        owner_user_id=user.id,
    )
    raw_members = _archive_member_texts(raw_archive)
    assert "runner_task_id" in raw_members["metadata.json"]
    assert "limira/users/hash" in raw_members["trace.json"]
    archive_key = limira.build_limira_object_key(
        owner_user_id=user.id,
        category="archives",
        task_id=task.task_id,
        filename="archive.zip",
        object_id=task.task_id,
    )
    stored = await storage.put_object(
        object_key=archive_key,
        data=raw_archive,
        content_type="application/zip",
        metadata={
            "task_id": task.task_id,
            "owner_user_id": user.id,
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
        },
    )
    repo.update_task(
        task.task_id,
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.body != raw_archive
    _assert_archive_hides_internal_model_summary_identifiers(response.body)
    assert task.archive_object_key == stored.object_key
    assert task.archive_zip_sha256 == hashlib.sha256(response.body).hexdigest()
    repaired = storage.objects[stored.object_key]
    assert repaired["data"] == response.body
    assert repaired["sha256"] == task.archive_zip_sha256
    assert repaired["metadata"]["archive_sha256"] == task.archive_zip_sha256
    _assert_archive_hides_internal_model_summary_identifiers(repaired["data"])


@pytest.mark.asyncio
async def test_postgres_archive_download_repairs_reused_persisted_model_summary_identifiers():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task_id = "task-postgres-summary-archive-reuse"
    repo.create_task(
        task_id=task_id,
        owner_user_id=user.id,
        query="postgres archive internal state reuse",
        scenario=None,
        runner_task_id="runner-task-secret",
    )
    raw_archive = _archive_zip_with_raw_model_summary(
        task_id=task_id,
        owner_user_id=user.id,
    )
    archive_key = limira.build_limira_object_key(
        owner_user_id=user.id,
        category="archives",
        task_id=task_id,
        filename="archive.zip",
        object_id=task_id,
    )
    stored = await storage.put_object(
        object_key=archive_key,
        data=raw_archive,
        content_type="application/zip",
        metadata={
            "task_id": task_id,
            "owner_user_id": user.id,
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
        },
    )
    repo.update_task(
        task_id,
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )

    response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.body != raw_archive
    _assert_archive_hides_internal_model_summary_identifiers(response.body)
    persisted = engine.tasks[task_id]
    assert persisted["archive_object_key"] == stored.object_key
    assert persisted["archive_zip_sha256"] == hashlib.sha256(response.body).hexdigest()
    repaired = storage.objects[stored.object_key]
    assert repaired["data"] == response.body
    assert repaired["sha256"] == persisted["archive_zip_sha256"]
    assert repaired["metadata"]["archive_sha256"] == persisted["archive_zip_sha256"]
    _assert_archive_hides_internal_model_summary_identifiers(repaired["data"])


@pytest.mark.asyncio
async def test_archive_download_regenerates_reused_archive_with_undecodable_text_member():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-binary-archive-reuse",
        owner_user_id=user.id,
        query="binary archive member",
        scenario=None,
        runner_task_id="runner-binary-archive",
    )
    raw_archive = _archive_zip_with_undecodable_report_member()
    with zipfile.ZipFile(io.BytesIO(raw_archive)) as raw_zip:
        assert b"sk-binarysecret123456" in raw_zip.read("report.html")
    archive_key = limira.build_limira_object_key(
        owner_user_id=user.id,
        category="archives",
        task_id=task.task_id,
        filename="archive.zip",
        object_id=task.task_id,
    )
    stored = await storage.put_object(
        object_key=archive_key,
        data=raw_archive,
        content_type="application/zip",
        metadata={
            "task_id": task.task_id,
            "owner_user_id": user.id,
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
        },
    )
    repo.update_task(
        task.task_id,
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.body != raw_archive
    members = _archive_member_texts(response.body)
    assert members["metadata.json"]
    assert "binary archive member" in members["report.md"]
    combined = "\n".join(members.values())
    assert "sk-binarysecret123456" not in combined
    assert task.archive_object_key == stored.object_key
    assert task.archive_zip_sha256 == hashlib.sha256(response.body).hexdigest()
    repaired = storage.objects[stored.object_key]
    assert repaired["data"] == response.body
    assert repaired["sha256"] == task.archive_zip_sha256
    assert repaired["metadata"]["archive_sha256"] == task.archive_zip_sha256


@pytest.mark.asyncio
async def test_postgres_archive_download_regenerates_reused_archive_with_undecodable_text_member():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task_id = "task-postgres-binary-archive-reuse"
    repo.create_task(
        task_id=task_id,
        owner_user_id=user.id,
        query="postgres binary archive member",
        scenario=None,
        runner_task_id="runner-postgres-binary-archive",
    )
    raw_archive = _archive_zip_with_undecodable_report_member()
    archive_key = limira.build_limira_object_key(
        owner_user_id=user.id,
        category="archives",
        task_id=task_id,
        filename="archive.zip",
        object_id=task_id,
    )
    stored = await storage.put_object(
        object_key=archive_key,
        data=raw_archive,
        content_type="application/zip",
        metadata={
            "task_id": task_id,
            "owner_user_id": user.id,
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
        },
    )
    repo.update_task(
        task_id,
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )

    response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.body != raw_archive
    combined = "\n".join(_archive_member_texts(response.body).values())
    assert "postgres binary archive member" in combined
    assert "sk-binarysecret123456" not in combined
    persisted = engine.tasks[task_id]
    assert persisted["archive_object_key"] == stored.object_key
    assert persisted["archive_zip_sha256"] == hashlib.sha256(response.body).hexdigest()
    repaired = storage.objects[stored.object_key]
    assert repaired["data"] == response.body
    assert repaired["sha256"] == persisted["archive_zip_sha256"]
    assert repaired["metadata"]["archive_sha256"] == persisted["archive_zip_sha256"]


@pytest.mark.asyncio
async def test_archive_download_regenerates_reused_archive_with_invalid_json_metadata_members():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-invalid-json-archive-reuse",
        owner_user_id=user.id,
        query="invalid json archive member",
        scenario=None,
        runner_task_id="runner-invalid-json-task",
    )
    raw_archive = _archive_zip_with_invalid_json_metadata_members(task.task_id)
    raw_members = _archive_member_texts(raw_archive)
    assert "runner_task_id=runner-invalid-json-123" in raw_members["metadata.json"]
    assert "limira/users/hash" in raw_members["trace.json"]
    archive_key = limira.build_limira_object_key(
        owner_user_id=user.id,
        category="archives",
        task_id=task.task_id,
        filename="archive.zip",
        object_id=task.task_id,
    )
    stored = await storage.put_object(
        object_key=archive_key,
        data=raw_archive,
        content_type="application/zip",
        metadata={
            "task_id": task.task_id,
            "owner_user_id": user.id,
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
        },
    )
    repo.update_task(
        task.task_id,
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.body != raw_archive
    members = _archive_member_texts(response.body)
    metadata = json.loads(members["metadata.json"])
    trace = json.loads(members["trace.json"])
    assert metadata["task"]["task_id"] == task.task_id
    assert trace["task"]["task_id"] == task.task_id
    _assert_archive_hides_invalid_json_member_identifiers(response.body)
    assert task.archive_object_key == stored.object_key
    assert task.archive_zip_sha256 == hashlib.sha256(response.body).hexdigest()
    repaired = storage.objects[stored.object_key]
    assert repaired["data"] == response.body
    assert repaired["sha256"] == task.archive_zip_sha256
    assert repaired["metadata"]["archive_sha256"] == task.archive_zip_sha256
    _assert_archive_hides_invalid_json_member_identifiers(repaired["data"])


@pytest.mark.asyncio
async def test_postgres_archive_download_regenerates_reused_archive_with_invalid_json_metadata_members():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task_id = "task-postgres-invalid-json-archive-reuse"
    repo.create_task(
        task_id=task_id,
        owner_user_id=user.id,
        query="postgres invalid json archive member",
        scenario=None,
        runner_task_id="runner-postgres-invalid-json-task",
    )
    raw_archive = _archive_zip_with_invalid_json_metadata_members(task_id)
    archive_key = limira.build_limira_object_key(
        owner_user_id=user.id,
        category="archives",
        task_id=task_id,
        filename="archive.zip",
        object_id=task_id,
    )
    stored = await storage.put_object(
        object_key=archive_key,
        data=raw_archive,
        content_type="application/zip",
        metadata={
            "task_id": task_id,
            "owner_user_id": user.id,
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
        },
    )
    repo.update_task(
        task_id,
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )

    response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.body != raw_archive
    members = _archive_member_texts(response.body)
    assert json.loads(members["metadata.json"])["task"]["task_id"] == task_id
    assert json.loads(members["trace.json"])["task"]["task_id"] == task_id
    _assert_archive_hides_invalid_json_member_identifiers(response.body)
    persisted = engine.tasks[task_id]
    assert persisted["archive_object_key"] == stored.object_key
    assert persisted["archive_zip_sha256"] == hashlib.sha256(response.body).hexdigest()
    repaired = storage.objects[stored.object_key]
    assert repaired["data"] == response.body
    assert repaired["sha256"] == persisted["archive_zip_sha256"]
    assert repaired["metadata"]["archive_sha256"] == persisted["archive_zip_sha256"]
    _assert_archive_hides_invalid_json_member_identifiers(repaired["data"])


@pytest.mark.asyncio
async def test_archive_download_regenerates_after_task_scoped_writes():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-fresh-archive",
        owner_user_id=user.id,
        query="fresh archive",
        scenario=None,
        runner_task_id="runner-fresh-archive",
    )
    task.archive_status = "ready"

    first_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    first_sha = task.archive_zip_sha256
    first_members = _archive_member_texts(first_response.body)
    assert "Fresh report" not in first_members["report.md"]

    repo.record_generated_report(
        report_id="report-fresh",
        task_id=task.task_id,
        report_type="final",
        markdown="Fresh report [EVID-FRESH]",
        html=None,
        pdf_object_key=None,
        evidence_refs=["EVID-FRESH"],
        creator_user_id=user.id,
        metadata={},
    )
    assert task.archive_object_key is None
    assert task.archive_zip_sha256 is None

    second_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    second_sha = task.archive_zip_sha256
    second_members = _archive_member_texts(second_response.body)
    assert second_sha != first_sha
    assert "Fresh report [EVID-FRESH]" in second_members["report.md"]

    repo.record_uploaded_document(
        document_id="doc-fresh",
        owner_user_id=user.id,
        task_id=task.task_id,
        original_filename="brief.txt",
        content_type="text/plain",
        byte_size=5,
        minio_bucket="limira-artifacts",
        object_key="limira/users/hash/tasks/task-fresh-archive/uploads/doc-fresh.txt",
        extracted_text="brief",
        language=None,
        metadata={},
    )
    assert task.archive_object_key is None
    assert task.archive_zip_sha256 is None

    third_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    third_sha = task.archive_zip_sha256
    third_trace = json.loads(_archive_member_texts(third_response.body)["trace.json"])
    assert third_sha != second_sha
    assert third_trace["uploaded_documents"][0]["document_id"] == "doc-fresh"

    repo.record_artifact(
        task.task_id,
        "evidence",
        {
            "evidence_id": "EVID-NEW",
            "summary": "new evidence",
            "source_url": "https://example.test/new",
        },
    )
    assert task.archive_object_key is None
    assert task.archive_zip_sha256 is None

    fourth_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    fourth_trace = json.loads(_archive_member_texts(fourth_response.body)["trace.json"])
    assert fourth_trace["artifacts"]["evidence"][0]["evidence_id"] == "EVID-NEW"
    assert task.archive_object_key in storage.objects
    assert storage.objects[task.archive_object_key]["content_type"] == "application/zip"


@pytest.mark.asyncio
async def test_archive_download_includes_evidence_web_snapshots(monkeypatch):
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    fetched_url = "https://example.test/port"

    async def fake_fetch_evidence_page_snapshots(_artifacts):
        return {
            fetched_url: limira.EvidencePageSnapshot(
                url=fetched_url,
                final_url="https://example.test/port?print=1",
                status_code=200,
                content_type="text/html; charset=utf-8",
                html=(
                    "<!doctype html><html><head><title>Original page</title>"
                    "<script>window.evil=true</script></head>"
                    '<body onload="steal()">'
                    "<article id=\"original\">Original archived page body.</article>"
                    '<a href="javascript:alert(1)">bad link</a>'
                    "</body></html>"
                ),
            )
        }

    monkeypatch.setattr(
        limira,
        "_fetch_evidence_page_snapshots",
        fake_fetch_evidence_page_snapshots,
    )
    task = repo.create_task(
        task_id="task-evidence-snapshots",
        owner_user_id=user.id,
        query="snapshot archive",
        scenario=None,
        runner_task_id="runner-evidence-snapshots",
    )
    repo.record_artifact(
        task.task_id,
        "evidence",
        {
            "evidence_id": "EVID-001",
            "title": "Port authority bulletin",
            "source": "Example Maritime",
            "source_url": "https://example.test/port",
            "published_at": "2026-06-09",
            "confidence": 0.9,
            "summary": "New inspection rule published",
        },
    )
    repo.record_task_event_log(
        task.task_id,
        {
            "type": "tool_result",
            "payload": {
                "tool_name": "web_fetch",
                "result": json.dumps(
                    {
                        "url": "https://example.test/port/",
                        "title": "Port authority bulletin",
                        "extracted_info": (
                            "Captured page text: terminal policy changed."
                        ),
                    }
                ),
            },
        },
    )
    repo.update_task(task.task_id, status="completed", archive_status="ready")

    archive_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    members = _archive_member_texts(archive_response.body)
    assert list(members)[:4] == list(limira.ARCHIVE_MEMBER_ORDER)
    assert "evidence_snapshots/manifest.json" in members
    manifest = json.loads(members["evidence_snapshots/manifest.json"])
    snapshot = manifest["snapshots"][0]
    assert snapshot["evidence_id"] == "EVID-001"
    assert snapshot["url"] == "https://example.test/port"
    assert snapshot["page_snapshot_available"] is True
    assert snapshot["page_snapshot_source"] == "fetched_url"
    assert snapshot["summary_source"] == "task_event_log"
    assert snapshot["page_status_code"] == 200
    page_html = members[snapshot["page_member_name"]]
    summary_html = members[snapshot["summary_member_name"]]
    assert "Limira archived webpage snapshot" in page_html
    assert "Original archived page body." in page_html
    assert "https://example.test/port?print=1" in page_html
    assert "<script" not in page_html.lower()
    assert "onload=" not in page_html.lower()
    assert "javascript:alert" not in page_html.lower()
    assert 'href="#"' in page_html
    assert "Port authority bulletin" in summary_html
    assert "https://example.test/port" in summary_html
    assert "Captured page text: terminal policy changed." in summary_html
    assert "New inspection rule published" in summary_html
    metadata = json.loads(members["metadata.json"])
    trace = json.loads(members["trace.json"])
    assert metadata["evidence_snapshots"] == manifest["snapshots"]
    assert trace["evidence_snapshots"] == manifest["snapshots"]
    assert task.archive_object_key in storage.objects
    assert storage.objects[task.archive_object_key]["data"] == archive_response.body
    _assert_no_browser_leak(members)


@pytest.mark.asyncio
async def test_archive_download_regenerates_reused_archive_missing_evidence_snapshots():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-missing-snapshot-reuse",
        owner_user_id=user.id,
        query="missing snapshot archive",
        scenario=None,
        runner_task_id="runner-missing-snapshot",
    )
    repo.record_artifact(
        task.task_id,
        "evidence",
        {
            "evidence_id": "EVID-REUSE",
            "title": "Reusable evidence",
            "source_url": "https://example.test/reuse",
            "summary": "Snapshot must be regenerated.",
        },
    )
    repo.update_task(task.task_id, status="completed", archive_status="ready")
    raw_archive = _archive_zip()
    assert "evidence_snapshots/manifest.json" not in _archive_member_texts(raw_archive)
    archive_key = limira.build_limira_object_key(
        owner_user_id=user.id,
        category="archives",
        task_id=task.task_id,
        filename="archive.zip",
        object_id=task.task_id,
    )
    stored = await storage.put_object(
        object_key=archive_key,
        data=raw_archive,
        content_type="application/zip",
        metadata={
            "task_id": task.task_id,
            "owner_user_id": user.id,
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
        },
    )
    repo.update_task(
        task.task_id,
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.body != raw_archive
    members = _archive_member_texts(response.body)
    manifest = json.loads(members["evidence_snapshots/manifest.json"])
    assert manifest["snapshots"][0]["evidence_id"] == "EVID-REUSE"
    snapshot = manifest["snapshots"][0]
    assert snapshot["page_snapshot_available"] is False
    page_html = members[snapshot["page_member_name"]]
    summary_html = members[snapshot["summary_member_name"]]
    assert "网页原始 HTML 快照不可用" in page_html
    assert "Reusable evidence" in summary_html
    assert "Snapshot must be regenerated." in summary_html
    assert task.archive_object_key == stored.object_key
    assert task.archive_zip_sha256 == hashlib.sha256(response.body).hexdigest()
    repaired = storage.objects[stored.object_key]
    assert repaired["data"] == response.body
    assert repaired["sha256"] == task.archive_zip_sha256
    assert repaired["metadata"]["archive_sha256"] == task.archive_zip_sha256


@pytest.mark.asyncio
async def test_untasked_upload_does_not_invalidate_task_archive():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-unlinked-upload",
        owner_user_id=user.id,
        query="unlinked upload",
        scenario=None,
        runner_task_id="runner-unlinked-upload",
    )
    task.archive_status = "ready"

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    archive_key = task.archive_object_key
    archive_sha = task.archive_zip_sha256

    repo.record_uploaded_document(
        document_id="doc-unlinked",
        owner_user_id=user.id,
        task_id=None,
        original_filename="unlinked.txt",
        content_type="text/plain",
        byte_size=8,
        minio_bucket="limira-artifacts",
        object_key="limira/users/hash/uploads/doc-unlinked.txt",
        extracted_text="unlinked",
        language=None,
        metadata={},
    )

    assert task.archive_object_key == archive_key
    assert task.archive_zip_sha256 == archive_sha
    second_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    assert second_response.body == response.body


@pytest.mark.asyncio
async def test_admin_access_requires_explicit_admin_route():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    research = FakeResearchClient()
    user = limira.LimiraUser("user-a")
    admin = limira.LimiraUser("admin-user", role="admin")

    created = await limira.create_research_task(
        {"query": "critical minerals policy"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.tasks[task_id].archive_status = "ready"

    with pytest.raises(HTTPException) as normal_admin_route:
        await limira.get_task(task_id, user=admin, repo=repo)
    assert normal_admin_route.value.status_code == 404

    payload = await limira.admin_get_task(task_id, user=admin, repo=repo)
    assert payload["owner_user_id"] == "user-a"
    assert payload["admin"] == "admin-user"

    response = await limira.admin_download_task_archive(
        task_id,
        user=admin,
        repo=repo,
        object_storage=storage,
    )
    assert response.media_type == "application/zip"
    assert repo.tasks[task_id].archive_object_key in storage.objects


@pytest.mark.asyncio
async def test_admin_task_event_logs_are_persisted_for_operations_only():
    repo = limira.InMemoryLimiraTaskRepository()
    research = FakeResearchClient(
        events=[
            {
                "event": "start_of_workflow",
                "data": {"message": "internal-looking user noise"},
            },
            {
                "event": "status",
                "data": {"status": "completed", "archive_status": "ready"},
            },
        ]
    )
    user = limira.LimiraUser("user-a")
    admin = limira.LimiraUser("admin-user", role="admin")
    created = await limira.create_research_task(
        {"query": "ops event log boundary"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    response = await limira.get_task_events(
        created["task_id"],
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    streamed_events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    payload = await limira.admin_get_task_event_logs(
        created["task_id"],
        limit=500,
        user=admin,
        repo=repo,
    )

    assert payload["admin"] == "admin-user"
    assert payload["task_id"] == created["task_id"]
    assert payload["count"] == len(payload["events"])
    assert [event["event_type"] for event in payload["events"]] == [
        event["type"] for event in streamed_events
    ]
    assert all(event["source"] == "runner_stream" for event in payload["events"])
    assert all(event["task_id"] == created["task_id"] for event in payload["events"])


@pytest.mark.asyncio
async def test_postgres_admin_archive_download_generates_owner_scoped_archive():
    app, _memory_repo, storage = _limira_asgi_app()
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )

    async def task_repository_override():
        return repo

    async def admin_user_override():
        return limira.LimiraUser("admin-user", role="admin")

    app.dependency_overrides[limira.get_task_repository] = task_repository_override
    app.dependency_overrides[limira.get_current_limira_admin] = admin_user_override

    task_id = "task-postgres-admin-archive"
    repo.create_task(
        task_id=task_id,
        owner_user_id="user-a",
        query="admin archive boundary",
        scenario=None,
        runner_task_id="runner-postgres-admin-archive",
    )
    repo.update_task(task_id, status="completed", archive_status="ready")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(f"/api/limira/admin/tasks/{task_id}/archive.zip")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["x-content-type-options"] == "nosniff"

    persisted = engine.tasks[task_id]
    archive_key = persisted["archive_object_key"]
    owner_digest = hashlib.sha256(b"user-a").hexdigest()[:24]
    admin_digest = hashlib.sha256(b"admin-user").hexdigest()[:24]
    assert f"/users/{owner_digest}/tasks/{task_id}/archives/" in archive_key
    assert admin_digest not in archive_key
    assert archive_key in storage.objects
    stored = storage.objects[archive_key]
    assert stored["content_type"] == "application/zip"
    assert stored["metadata"]["owner_user_id"] == "user-a"
    assert stored["metadata"]["generated_by"] == "admin-user"
    assert persisted["archive_zip_sha256"] == hashlib.sha256(response.content).hexdigest()
    assert stored["sha256"] == persisted["archive_zip_sha256"]

    members = _archive_member_texts(response.content)
    metadata = json.loads(members["metadata.json"])
    trace = json.loads(members["trace.json"])
    assert metadata["task"]["task_id"] == task_id
    assert trace["task"]["task_id"] == task_id
    _assert_public_archive_hides_internal_identifiers(response.content)
    assert "admin-user" not in "\n".join(members.values())
    _assert_no_browser_leak(members)


@pytest.mark.asyncio
async def test_task_payload_hides_internal_model_summary_identifiers():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    admin = limira.LimiraUser("admin-user", role="admin")
    task = repo.create_task(
        task_id="task-summary",
        owner_user_id=user.id,
        query="summarize internal state",
        scenario=None,
        runner_task_id="runner-task-secret",
    )
    repo.update_task(
        task.task_id,
        model_summary={
            "provider": "deepseek",
            "runner_task_id": "runner-task-secret",
            "object_key": "limira/users/hash/tasks/task-summary/uploads/doc.txt",
            "endpoint": (
                "http://10.20.30.40:8091/limira-runner/tasks/runner-task-secret"
            ),
            "nested": {
                "archive_object_key": (
                    "limira/users/hash/tasks/task-summary/archives/archive.zip"
                ),
                "safe": "kept",
                "warning": "limira/users/hash/tasks/task-summary/uploads/doc.txt",
            },
        },
    )

    user_payload = await limira.get_task(task.task_id, user=user, repo=repo)
    admin_payload = await limira.admin_get_task(task.task_id, user=admin, repo=repo)

    for payload in (user_payload, admin_payload):
        assert payload["model_summary"]["provider"] == "deepseek"
        assert payload["model_summary"]["nested"]["safe"] == "kept"
        assert payload["model_summary"]["endpoint"] == "limira_internal_value_redacted"
        assert payload["model_summary"]["nested"]["warning"] == (
            "limira_internal_value_redacted"
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for leaked in (
            "runner_task_id",
            "runner-task-secret",
            "object_key",
            "archive_object_key",
            "limira/users/hash",
            "http://10.20.30.40:8091",
            "/limira-runner/",
        ):
            assert leaked not in serialized
        _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_postgres_task_detail_route_is_owner_scoped_and_hides_internal_identifiers():
    app, _repo, _storage = _limira_asgi_app()
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )

    async def task_repository_override():
        return repo

    app.dependency_overrides[limira.get_task_repository] = task_repository_override
    owned_task = repo.create_task(
        task_id="task-owned-detail",
        owner_user_id="user-a",
        query="summarize internal state",
        scenario=None,
        runner_task_id="runner-task-secret",
    )
    repo.update_task(
        owned_task.task_id,
        status="completed",
        archive_status="ready",
        archive_object_key=(
            "limira/users/user-a/tasks/task-owned-detail/archives/archive.zip"
        ),
        archive_zip_sha256="a" * 64,
        model_summary=_internal_model_summary(
            owned_task.task_id,
            runner_task_id="runner-task-secret",
        ),
    )
    foreign_task = repo.create_task(
        task_id="task-foreign-detail",
        owner_user_id="user-b",
        query="foreign internal state",
        scenario=None,
        runner_task_id="runner-foreign-secret",
    )
    repo.update_task(
        foreign_task.task_id,
        status="completed",
        archive_status="ready",
        archive_object_key=(
            "limira/users/user-b/tasks/task-foreign-detail/archives/archive.zip"
        ),
        archive_zip_sha256="b" * 64,
        model_summary=_internal_model_summary(
            foreign_task.task_id,
            runner_task_id="runner-foreign-secret",
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        owned_response = await client.get(
            "/api/limira/tasks/task-owned-detail",
        )
        foreign_response = await client.get(
            "/api/limira/tasks/task-foreign-detail",
        )

    assert owned_response.status_code == 200
    payload = owned_response.json()
    assert payload["task_id"] == "task-owned-detail"
    assert payload["status"] == "completed"
    assert payload["archive_status"] == "ready"
    assert payload["download_url"] == (
        "/api/limira/tasks/task-owned-detail/archive.zip"
    )
    assert "owner_user_id" not in payload
    assert payload["model_summary"]["provider"] == "deepseek"
    assert payload["model_summary"]["nested"]["safe"] == "kept"
    assert payload["model_summary"]["endpoint"] == "limira_internal_value_redacted"
    assert payload["model_summary"]["nested"]["warning"] == (
        "limira_internal_value_redacted"
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    for leaked in (
        "runner_task_id",
        "runner-task-secret",
        "runner-foreign-secret",
        "object_key",
        "archive_object_key",
        "archive_zip_sha256",
        "limira/users/",
        "http://10.20.30.40:8091",
        "/limira-runner/",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(payload)

    assert foreign_response.status_code == 404
    assert foreign_response.json()["detail"] == "task_not_found"
    serialized_foreign = json.dumps(foreign_response.json(), ensure_ascii=False)
    for leaked in (
        "task-foreign-detail",
        "runner-foreign-secret",
        "user-b",
        "limira/users/user-b",
        "http://10.20.30.40:8091",
        "/limira-runner/",
    ):
        assert leaked not in serialized_foreign
    _assert_no_browser_leak(foreign_response.json())


@pytest.mark.asyncio
async def test_postgres_task_history_route_is_owner_scoped_and_public_serialized():
    app, _repo, _storage = _limira_asgi_app()
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )

    async def task_repository_override():
        return repo

    app.dependency_overrides[limira.get_task_repository] = task_repository_override

    older_task = repo.create_task(
        task_id="task-history-older",
        owner_user_id="user-a",
        query="older user task",
        scenario="sanctions_export_controls",
        runner_task_id="runner-history-older-secret",
    )
    repo.update_task(
        older_task.task_id,
        status="running",
        model_summary=_internal_model_summary(
            older_task.task_id,
            runner_task_id="runner-history-older-secret",
        ),
    )
    foreign_task = repo.create_task(
        task_id="task-history-foreign",
        owner_user_id="user-b",
        query="foreign user task",
        scenario=None,
        runner_task_id="runner-history-foreign-secret",
    )
    repo.update_task(
        foreign_task.task_id,
        status="completed",
        archive_status="ready",
        archive_object_key=(
            "limira/users/user-b/tasks/task-history-foreign/archives/archive.zip"
        ),
        archive_zip_sha256="b" * 64,
        model_summary=_internal_model_summary(
            foreign_task.task_id,
            runner_task_id="runner-history-foreign-secret",
        ),
    )
    latest_task = repo.create_task(
        task_id="task-history-latest",
        owner_user_id="user-a",
        query="latest user task",
        scenario=None,
        runner_task_id="runner-history-latest-secret",
    )
    repo.update_task(
        latest_task.task_id,
        status="completed",
        archive_status="ready",
        archive_object_key=(
            "limira/users/user-a/tasks/task-history-latest/archives/archive.zip"
        ),
        archive_zip_sha256="a" * 64,
        model_summary=_internal_model_summary(
            latest_task.task_id,
            runner_task_id="runner-history-latest-secret",
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get("/api/limira/tasks", params={"limit": 10})
        limited_response = await client.get("/api/limira/tasks", params={"limit": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert [task["task_id"] for task in payload["tasks"]] == [
        "task-history-latest",
        "task-history-older",
    ]
    assert payload["tasks"][0]["download_url"] == (
        "/api/limira/tasks/task-history-latest/archive.zip"
    )
    assert payload["tasks"][1]["download_url"] is None
    serialized = json.dumps(payload, ensure_ascii=False)
    for leaked in (
        "owner_user_id",
        "runner_task_id",
        "runner-history-latest-secret",
        "runner-history-older-secret",
        "runner-history-foreign-secret",
        "task-history-foreign",
        "user-b",
        "object_key",
        "archive_object_key",
        "archive_zip_sha256",
        "limira/users/",
        "http://10.20.30.40:8091",
        "/limira-runner/",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(payload)

    assert limited_response.status_code == 200
    assert limited_response.json()["count"] == 1
    assert limited_response.json()["tasks"][0]["task_id"] == "task-history-latest"


@pytest.mark.asyncio
async def test_archive_proxy_rejects_not_ready_and_invalid_zip_members():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    research = FakeResearchClient()
    user = limira.LimiraUser("user-a")
    created = await limira.create_research_task(
        {"query": "query"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    with pytest.raises(HTTPException) as not_ready:
        await limira.download_task_archive(
            task_id,
            user=user,
            repo=repo,
            object_storage=storage,
        )
    assert not_ready.value.status_code == 409

    repo.tasks[task_id].archive_status = "ready"
    with pytest.raises(HTTPException) as invalid_zip:
        limira.validate_archive_zip(_archive_zip(extra_member=True))
    assert invalid_zip.value.status_code == 502


def test_runner_service_headers_are_server_side_only():
    headers = limira.runner_service_headers(
        limira.LimiraUser("user-a", role="admin"),
        "server-only-token",
    )

    assert headers == {
        "X-Limira-User-Id": "user-a",
        "X-Limira-User-Role": "admin",
        "X-Limira-Runner-Service-Token": "server-only-token",
    }


def test_limira_repository_factory_requires_explicit_memory_fallback(monkeypatch):
    monkeypatch.delenv("LIMIRA_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LIMIRA_REPOSITORY_BACKEND", "postgres")

    with pytest.raises(RuntimeError, match="limira_postgres_database_url_missing"):
        limira.create_limira_task_repository_from_env()

    monkeypatch.setenv("LIMIRA_DATABASE_URL", "postgresql://limira:test@postgres:5432/limira")
    repo = limira.create_limira_task_repository_from_env()
    assert isinstance(repo, limira.PostgresLimiraTaskRepository)
    assert repo.database_url == "postgresql://limira:test@postgres:5432/limira"
    assert repo._engine is None

    monkeypatch.setenv("LIMIRA_REPOSITORY_BACKEND", "memory")
    monkeypatch.delenv("LIMIRA_ALLOW_IN_MEMORY_REPOSITORY", raising=False)
    with pytest.raises(RuntimeError, match="limira_in_memory_repository_requires_explicit_fallback"):
        limira.create_limira_task_repository_from_env()

    monkeypatch.setenv("LIMIRA_ALLOW_IN_MEMORY_REPOSITORY", "true")
    repo = limira.create_limira_task_repository_from_env()
    assert isinstance(repo, limira.InMemoryLimiraTaskRepository)


def test_sqlite_limira_repository_persists_task_artifacts_reports_and_uploads(tmp_path):
    database_path = tmp_path / "limira.sqlite3"
    repo = limira.SQLiteLimiraTaskRepository(str(database_path))
    task = repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="query",
        scenario="scenario-a",
        runner_task_id="runner-a",
    )
    repo.update_task(task.task_id, status="completed", archive_status="ready")
    repo.record_artifact(
        task.task_id,
        "evidence",
        {
            "evidence_id": "EVID-001",
            "title": "Source",
            "summary": "Persistent evidence",
        },
    )
    repo.record_artifact_trace_event(
        task.task_id,
        {
            "type": "artifact_warning",
            "payload": {"warning": "nonfatal"},
        },
    )
    repo.record_uploaded_document(
        document_id="doc-a",
        owner_user_id="user-a",
        task_id=task.task_id,
        original_filename="memo.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket="bucket",
        object_key="limira/users/u/tasks/task-a/uploads/doc-a.txt",
        extracted_text="Persistent memo text",
        language="en",
        metadata={"sha256": "abc"},
        embedding=[1.0, 0.0],
    )
    repo.record_generated_report(
        report_id="report-a",
        task_id=task.task_id,
        report_type="final",
        markdown="## Final\n\nPersistent report",
        html=None,
        pdf_object_key="limira/users/u/tasks/task-a/reports/report-a.pdf",
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={"pdf_size_bytes": 123},
    )

    restored = limira.SQLiteLimiraTaskRepository(str(database_path))
    restored_task = restored.get_user_task("task-a", "user-a")
    assert restored_task is not None
    assert restored_task.status == "completed"
    assert restored.get_artifacts("task-a")["evidence"][0]["evidence_id"] == "EVID-001"
    assert restored.get_artifact_trace_events("task-a")[-1]["type"] == "artifact_warning"
    assert restored.list_user_documents(owner_user_id="user-a", task_id="task-a")[0].document_id == "doc-a"
    assert restored.search_user_documents(
        owner_user_id="user-a",
        task_id="task-a",
        query="memo",
        limit=5,
    )[0].document.document_id == "doc-a"
    assert restored.get_user_report(
        task_id="task-a",
        report_id="report-a",
        owner_user_id="user-a",
    ).pdf_object_key.endswith("report-a.pdf")


def test_limira_runtime_state_factory_requires_redis_or_explicit_memory(monkeypatch):
    monkeypatch.setenv("LIMIRA_RUNTIME_STATE_BACKEND", "redis")
    with pytest.raises(RuntimeError, match="limira_redis_runtime_state_missing"):
        limira.create_limira_runtime_state_from_env(redis_client=None)

    redis = FakeRedisClient()
    runtime_state = limira.create_limira_runtime_state_from_env(redis_client=redis)
    assert isinstance(runtime_state, limira.RedisLimiraRuntimeState)
    assert runtime_state.redis_client is redis

    monkeypatch.setenv("LIMIRA_RUNTIME_STATE_BACKEND", "memory")
    monkeypatch.delenv("LIMIRA_ALLOW_IN_MEMORY_RUNTIME_STATE", raising=False)
    with pytest.raises(
        RuntimeError,
        match="limira_in_memory_runtime_state_requires_explicit_fallback",
    ):
        limira.create_limira_runtime_state_from_env(redis_client=None)

    monkeypatch.setenv("LIMIRA_ALLOW_IN_MEMORY_RUNTIME_STATE", "true")
    runtime_state = limira.create_limira_runtime_state_from_env(redis_client=None)
    assert isinstance(runtime_state, limira.InMemoryLimiraRuntimeState)


def test_limira_object_storage_factory_requires_s3_or_explicit_memory(monkeypatch):
    for key in (
        "LIMIRA_OBJECT_BUCKET",
        "S3_BUCKET",
        "MINIO_BUCKET",
        "S3_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LIMIRA_OBJECT_STORAGE_BACKEND", "s3")

    with pytest.raises(RuntimeError, match="limira_object_bucket_missing"):
        limira.create_limira_object_storage_from_env(s3_client=FakeS3Client())

    monkeypatch.setenv("LIMIRA_OBJECT_BUCKET", "limira-artifacts")
    with pytest.raises(RuntimeError, match="limira_s3_endpoint_url_missing"):
        limira.create_limira_object_storage_from_env(s3_client=FakeS3Client())

    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    with pytest.raises(RuntimeError, match="limira_s3_credentials_missing"):
        limira.create_limira_object_storage_from_env(s3_client=FakeS3Client())

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "limira_minio")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "replace-with-local-minio-password")
    storage = limira.create_limira_object_storage_from_env(s3_client=FakeS3Client())
    assert isinstance(storage, limira.S3LimiraObjectStorage)
    assert storage.bucket == "limira-artifacts"
    assert storage.endpoint_url == "http://minio:9000"

    monkeypatch.setenv("LIMIRA_OBJECT_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("LIMIRA_ALLOW_IN_MEMORY_OBJECT_STORAGE", raising=False)
    with pytest.raises(
        RuntimeError,
        match="limira_in_memory_object_storage_requires_explicit_fallback",
    ):
        limira.create_limira_object_storage_from_env()

    monkeypatch.setenv("LIMIRA_ALLOW_IN_MEMORY_OBJECT_STORAGE", "true")
    storage = limira.create_limira_object_storage_from_env()
    assert isinstance(storage, limira.InMemoryLimiraObjectStorage)


@pytest.mark.asyncio
async def test_filesystem_object_storage_persists_objects_across_instances(tmp_path):
    storage = limira.FileSystemLimiraObjectStorage(root_path=str(tmp_path), bucket="local")
    stored = await storage.put_object(
        object_key="limira/users/u/tasks/task-a/reports/report-a.pdf",
        data=b"%PDF-test",
        content_type="application/pdf",
        metadata={"report_id": "report-a"},
    )

    restored = limira.FileSystemLimiraObjectStorage(root_path=str(tmp_path), bucket="local")
    assert stored.bucket == "local"
    assert await restored.get_object(object_key=stored.object_key) == b"%PDF-test"


@pytest.mark.asyncio
async def test_filesystem_object_storage_rejects_unsafe_object_keys(tmp_path):
    storage = limira.FileSystemLimiraObjectStorage(root_path=str(tmp_path), bucket="local")
    unsafe_keys = [
        "",
        "/absolute/object.txt",
        "../outside.txt",
        "limira/users/u/../secret.txt",
        "limira/users/u/a/../../secret.txt",
        "limira/users/u/./object.txt",
        "limira/users/u//object.txt",
        "limira\\users\\u\\object.txt",
        "limira/users/u/object.metadata.json",
        "limira/users/u/object.metadata.json/nested.txt",
    ]

    for object_key in unsafe_keys:
        with pytest.raises(ValueError, match="invalid_limira_object_key"):
            await storage.put_object(
                object_key=object_key,
                data=b"unsafe",
                content_type="text/plain",
                metadata={"document_id": "doc-a"},
            )
        with pytest.raises(ValueError, match="invalid_limira_object_key"):
            await storage.get_object(object_key=object_key)

    assert [path.name for path in tmp_path.iterdir()] == []


@pytest.mark.asyncio
async def test_object_storage_backends_validate_and_normalize_object_keys(tmp_path):
    unsafe_keys = [
        "",
        "/absolute/object.txt",
        "../outside.txt",
        "limira/users/u/../secret.txt",
        "limira/users/u/a/../../secret.txt",
        "limira/users/u/./object.txt",
        "limira/users/u//object.txt",
        "limira\\users\\u\\object.txt",
        "limira/users/u/object.metadata.json",
        "limira/users/u/object.metadata.json/nested.txt",
    ]
    safe_key = "limira/users/u/uploads/document.txt"
    whitespace_key = f"  {safe_key}  "

    memory_storage = limira.InMemoryLimiraObjectStorage(bucket="memory")
    filesystem_storage = limira.FileSystemLimiraObjectStorage(
        root_path=str(tmp_path / "objects"),
        bucket="local",
    )
    s3_client = FakeS3Client()
    s3_storage = limira.S3LimiraObjectStorage(
        bucket="limira-artifacts",
        endpoint_url="http://minio:9000",
        access_key_id="limira_minio",
        secret_access_key="replace-with-local-minio-password",
        s3_client=s3_client,
    )

    for storage in [memory_storage, filesystem_storage, s3_storage]:
        stored = await storage.put_object(
            object_key=whitespace_key,
            data=b"safe-bytes",
            content_type="text/plain",
            metadata={"document_id": "doc-a"},
        )
        assert stored.object_key == safe_key
        assert await storage.get_object(object_key=whitespace_key) == b"safe-bytes"

    assert list(memory_storage.objects) == [safe_key]
    assert s3_client.put_calls[-1]["Key"] == safe_key
    assert s3_client.get_calls[-1]["Key"] == safe_key

    for storage in [memory_storage, filesystem_storage, s3_storage]:
        for object_key in unsafe_keys:
            with pytest.raises(ValueError, match="invalid_limira_object_key"):
                await storage.put_object(
                    object_key=object_key,
                    data=b"unsafe",
                    content_type="text/plain",
                    metadata={"document_id": "doc-a"},
                )
            with pytest.raises(ValueError, match="invalid_limira_object_key"):
                await storage.get_object(object_key=object_key)

    assert list(memory_storage.objects) == [safe_key]
    assert [call["Key"] for call in s3_client.put_calls] == [safe_key]
    assert [call["Key"] for call in s3_client.get_calls] == [safe_key]


def test_limira_upload_embedding_config_defaults_disabled_and_validates_enabled():
    config = limira.create_limira_upload_embedding_config_from_env({})
    assert config == limira.LimiraUploadEmbeddingConfig(
        enabled=False,
        provider="disabled",
        model="",
        dimensions=1536,
    )

    enabled_env = {
        "LIMIRA_UPLOAD_EMBEDDINGS_ENABLED": "true",
        "LIMIRA_EMBEDDING_PROVIDER": "fake",
        "LIMIRA_EMBEDDING_MODEL": "fake-model",
        "LIMIRA_EMBEDDING_DIMENSIONS": "1536",
    }
    enabled = limira.create_limira_upload_embedding_config_from_env(enabled_env)
    assert enabled.enabled is True
    assert enabled.provider == "fake"
    assert enabled.model == "fake-model"
    assert enabled.dimensions == 1536

    with pytest.raises(RuntimeError, match="limira_upload_embedding_provider_required"):
        limira.create_limira_upload_embedding_config_from_env(
            {"LIMIRA_UPLOAD_EMBEDDINGS_ENABLED": "true"}
        )
    with pytest.raises(RuntimeError, match="limira_upload_embedding_model_required"):
        limira.create_limira_upload_embedding_config_from_env(
            {
                "LIMIRA_UPLOAD_EMBEDDINGS_ENABLED": "true",
                "LIMIRA_EMBEDDING_PROVIDER": "fake",
            }
        )
    with pytest.raises(RuntimeError, match="limira_upload_embedding_dimensions_invalid"):
        limira.create_limira_upload_embedding_config_from_env(
            {"LIMIRA_EMBEDDING_DIMENSIONS": "0"}
        )
    with pytest.raises(
        RuntimeError,
        match="limira_upload_embedding_dimensions_schema_mismatch",
    ):
        limira.create_limira_upload_embedding_config_from_env(
            {
                "LIMIRA_UPLOAD_EMBEDDINGS_ENABLED": "true",
                "LIMIRA_EMBEDDING_PROVIDER": "fake",
                "LIMIRA_EMBEDDING_MODEL": "fake-model",
                "LIMIRA_EMBEDDING_DIMENSIONS": "3",
            }
        )


def test_upload_embedding_dependencies_avoid_threadpool_for_default_route_path():
    assert asyncio.iscoroutinefunction(limira.get_upload_embedding_config)
    assert asyncio.iscoroutinefunction(limira.get_upload_embedding_provider)


def test_limira_object_keys_are_server_generated_owner_scoped_and_safe():
    key = limira.build_limira_object_key(
        owner_user_id="analyst@example.com",
        category="uploads",
        task_id="../task/alpha",
        filename="../../secret.env",
        object_id="../browser-supplied-key",
        key_prefix="../limira",
    )

    assert key.startswith("limira/users/")
    assert "/tasks/task-alpha/uploads/" in key
    assert key.endswith("browser-supplied-key.bin")
    assert "analyst@example.com" not in key
    assert "secret.env" not in key
    assert ".." not in key
    assert not key.startswith("/")

    report_key = limira.build_limira_object_key(
        owner_user_id="analyst@example.com",
        category="reports",
        task_id="task-a",
        extension="html",
        object_id="report-a",
    )
    assert report_key.endswith("/reports/report-a.html")

    with pytest.raises(ValueError, match="unsupported_limira_object_category"):
        limira.build_limira_object_key(
            owner_user_id="analyst@example.com",
            category="secrets",
        )


@pytest.mark.asyncio
async def test_s3_object_storage_put_uses_server_key_bucket_and_metadata():
    s3 = FakeS3Client()
    storage = limira.S3LimiraObjectStorage(
        bucket="limira-artifacts",
        endpoint_url="http://minio:9000",
        access_key_id="limira_minio",
        secret_access_key="replace-with-local-minio-password",
        s3_client=s3,
    )
    object_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="archives",
        task_id="task-a",
        filename="archive.zip",
        object_id="archive-a",
    )

    stored = await storage.put_object(
        object_key=object_key,
        data=b"archive-bytes",
        content_type="application/zip",
        metadata={"task_id": "task-a", "none": None, "unsafe key": "value"},
    )

    assert stored.object_key == object_key
    assert stored.bucket == "limira-artifacts"
    assert stored.size_bytes == len(b"archive-bytes")
    assert stored.sha256
    assert stored.metadata == {"task_id": "task-a", "unsafe-key": "value"}
    assert s3.put_calls == [
        {
            "Bucket": "limira-artifacts",
            "Key": object_key,
            "Body": b"archive-bytes",
            "ContentType": "application/zip",
            "Metadata": {"task_id": "task-a", "unsafe-key": "value"},
        }
    ]


@pytest.mark.asyncio
async def test_upload_route_rejects_object_key_aliases_on_actual_http_surface():
    app, _repo, _storage = _limira_asgi_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        assert set(PLAN_NAMED_OBJECT_KEY_ALIASES).issubset(
            limira.OBJECT_KEY_FORBIDDEN_FIELDS
        )
        aliases = sorted(
            set(limira.OBJECT_KEY_FORBIDDEN_FIELDS)
            | set(PLAN_NAMED_OBJECT_KEY_ALIASES)
        )
        for alias in aliases:
            query_response = await client.post(
                "/api/limira/uploads",
                params={alias: "users/user-a/uploads/evil.pdf"},
                files={"file": ("evidence.txt", b"evidence", "text/plain")},
            )
            assert query_response.status_code == 400, alias
            assert query_response.json()["detail"] == "object_key_server_generated"

            form_response = await client.post(
                "/api/limira/uploads",
                data={alias: "users/user-a/uploads/evil.pdf"},
                files={"file": ("evidence.txt", b"evidence", "text/plain")},
            )
            assert form_response.status_code == 400, alias
            assert form_response.json()["detail"] == "object_key_server_generated"

        empty_query_response = await client.post(
            "/api/limira/uploads",
            params={"object_key": ""},
            files={"file": ("evidence.txt", b"evidence", "text/plain")},
        )
        assert empty_query_response.status_code == 400
        assert empty_query_response.json()["detail"] == "object_key_server_generated"

        duplicate_query_response = await client.post(
            "/api/limira/uploads?object_key=users/user-a/uploads/evil.pdf&object_key=",
            files={"file": ("evidence.txt", b"evidence", "text/plain")},
        )
        assert duplicate_query_response.status_code == 400
        assert duplicate_query_response.json()["detail"] == "object_key_server_generated"

        empty_form_response = await client.post(
            "/api/limira/uploads",
            data={"object_key": ""},
            files={"file": ("evidence.txt", b"evidence", "text/plain")},
        )
        assert empty_form_response.status_code == 400
        assert empty_form_response.json()["detail"] == "object_key_server_generated"


@pytest.mark.asyncio
async def test_upload_route_stores_text_original_and_document_record():
    app, repo, storage = _limira_asgi_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/uploads",
            files={"file": ("evidence.txt", b"hello limira", "text/plain")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["filename"] == "evidence.txt"
    assert payload["content_type"] == "text/plain"
    assert payload["byte_size"] == len(b"hello limira")
    assert payload["extracted_text_chars"] == len("hello limira")
    assert "object_key" not in payload
    assert "minio_object_key" not in payload

    document = repo.get_user_document(payload["document_id"], "user-a")
    assert document is not None
    assert document.owner_user_id == "user-a"
    assert document.task_id is None
    assert document.extracted_text == "hello limira"
    assert document.embedding is None
    assert document.metadata["embedding"] == {
        "enabled": False,
        "provider": "disabled",
        "model": "",
        "dimensions": 1536,
        "status": "disabled",
    }
    assert document.minio_bucket == storage.bucket
    assert document.object_key in storage.objects
    stored = storage.objects[document.object_key]
    assert stored["data"] == b"hello limira"
    assert stored["content_type"] == "text/plain"
    assert stored["metadata"]["document_id"] == document.document_id
    assert stored["metadata"]["owner_user_id"] == "user-a"
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_upload_route_rejects_active_or_unsupported_content_types():
    app, repo, storage = _limira_asgi_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        for filename, content_type in [
            ("active.html", "text/html; charset=utf-8"),
            ("active.svg", "image/svg+xml"),
            ("active.xml", "application/xml"),
            ("active.js", "application/javascript"),
            ("unknown.bin", "application/octet-stream"),
        ]:
            response = await client.post(
                "/api/limira/uploads",
                files={
                    "file": (
                        filename,
                        b"<script>alert(1)</script>",
                        content_type,
                    )
                },
            )
            assert response.status_code == 415, (filename, content_type)
            assert response.json()["detail"] == "unsupported_upload_type"

        markdown_response = await client.post(
            "/api/limira/uploads",
            files={
                "file": (
                    "source.md",
                    b"# inert markdown",
                    "application/octet-stream",
                )
            },
        )

    assert markdown_response.status_code == 201
    assert markdown_response.json()["content_type"] == "text/markdown"
    assert len(repo.uploaded_documents) == 1
    assert len(storage.objects) == 1
    document = next(iter(repo.uploaded_documents.values()))
    assert document.original_filename == "source.md"
    assert document.content_type == "text/markdown"


@pytest.mark.asyncio
async def test_upload_route_records_configured_embedding_with_fake_provider():
    app, repo, _storage = _limira_asgi_app()
    provider = FakeUploadEmbeddingProvider([0.25, 0.5, 0.75])

    async def embedding_config_override():
        return limira.LimiraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=3,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limira.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limira.get_upload_embedding_provider] = (
        embedding_provider_override
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/uploads",
            files={"file": ("embedding.txt", b"lithium graphite source", "text/plain")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert "embedding" not in payload
    document = repo.get_user_document(payload["document_id"], "user-a")
    assert document is not None
    assert document.embedding == [0.25, 0.5, 0.75]
    assert provider.calls == [
        {
            "text": "lithium graphite source",
            "config": limira.LimiraUploadEmbeddingConfig(
                enabled=True,
                provider="fake",
                model="fake-model",
                dimensions=3,
            ),
        }
    ]
    assert document.metadata["embedding"] == {
        "enabled": True,
        "provider": "fake",
        "model": "fake-model",
        "dimensions": 3,
        "status": "stored",
    }
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_upload_route_rejects_embedding_dimension_mismatch_without_writes():
    app, repo, storage = _limira_asgi_app()
    provider = FakeUploadEmbeddingProvider([0.25, 0.5])

    async def embedding_config_override():
        return limira.LimiraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=3,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limira.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limira.get_upload_embedding_provider] = (
        embedding_provider_override
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/uploads",
            files={"file": ("bad-vector.txt", b"dimension mismatch", "text/plain")},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "upload_embedding_dimension_mismatch"
    assert storage.objects == {}
    assert repo.list_user_documents(owner_user_id="user-a") == []


@pytest.mark.asyncio
async def test_upload_route_links_only_owned_tasks():
    app, repo, storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="owned task",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    repo.create_task(
        task_id="task-b",
        owner_user_id="user-b",
        query="foreign task",
        scenario=None,
        runner_task_id="runner-task-b",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        owned_response = await client.post(
            "/api/limira/uploads",
            data={"task_id": "task-a"},
            files={"file": ("owned.md", b"# owned", "text/markdown")},
        )
        forbidden_response = await client.post(
            "/api/limira/uploads",
            data={"task_id": "task-b"},
            files={"file": ("foreign.md", b"# foreign", "text/markdown")},
        )
        owned_list_response = await client.get(
            "/api/limira/uploads",
            params={"task_id": "task-a"},
        )
        foreign_list_response = await client.get(
            "/api/limira/uploads",
            params={"task_id": "task-b"},
        )

    assert owned_response.status_code == 201
    payload = owned_response.json()
    assert payload["task_id"] == "task-a"
    document = repo.get_user_document(payload["document_id"], "user-a")
    assert document is not None
    assert document.task_id == "task-a"
    assert "/tasks/task-a/uploads/" in document.object_key
    assert storage.objects[document.object_key]["metadata"]["task_id"] == "task-a"
    assert owned_list_response.status_code == 200
    assert [doc["document_id"] for doc in owned_list_response.json()["documents"]] == [
        document.document_id
    ]
    assert foreign_list_response.status_code == 404
    assert forbidden_response.status_code == 404
    assert len(storage.objects) == 1


@pytest.mark.asyncio
async def test_upload_route_rejects_conflicting_form_and_query_task_ids():
    app, repo, storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="owned task",
        scenario=None,
        runner_task_id="runner-task-a",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/uploads",
            params={"task_id": "task-a"},
            data={"task_id": "task-other"},
            files={"file": ("conflict.txt", b"conflict", "text/plain")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "conflicting_task_id"
    assert storage.objects == {}
    assert repo.list_user_documents(owner_user_id="user-a") == []


@pytest.mark.asyncio
async def test_upload_route_stores_pdf_with_extracted_text(monkeypatch):
    app, repo, storage = _limira_asgi_app()
    monkeypatch.setattr(limira, "_extract_pdf_text", lambda data: "pdf extracted text")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/uploads",
            files={"file": ("brief.pdf", b"%PDF-1.7", "application/pdf")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["filename"] == "brief.pdf"
    assert payload["content_type"] == "application/pdf"
    assert payload["extracted_text_chars"] == len("pdf extracted text")
    document = repo.get_user_document(payload["document_id"], "user-a")
    assert document is not None
    assert document.extracted_text == "pdf extracted text"
    assert document.object_key.endswith(f"/uploads/{document.document_id}.pdf")
    assert storage.objects[document.object_key]["data"] == b"%PDF-1.7"


@pytest.mark.asyncio
async def test_upload_document_list_read_and_download_are_owner_scoped():
    app, repo, storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="owned task",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    repo.create_task(
        task_id="task-b",
        owner_user_id="user-b",
        query="foreign task",
        scenario=None,
        runner_task_id="runner-task-b",
    )
    foreign = repo.record_uploaded_document(
        document_id="foreign-doc",
        owner_user_id="user-b",
        task_id="task-b",
        original_filename="foreign.txt",
        content_type="text/plain",
        byte_size=7,
        minio_bucket=storage.bucket,
        object_key="limira/users/foreign/tasks/task-b/uploads/foreign.txt",
        extracted_text="foreign",
        language=None,
        metadata={},
    )
    storage.objects[foreign.object_key] = {
        "data": b"foreign",
        "content_type": "text/plain",
        "metadata": {},
        "sha256": "foreign",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        unlinked_response = await client.post(
            "/api/limira/uploads",
            files={"file": ("unlinked.txt", b"unlinked", "text/plain")},
        )
        linked_response = await client.post(
            "/api/limira/uploads",
            params={"task_id": "task-a"},
            files={"file": ("linked.txt", b"linked", "text/plain")},
        )

        list_response = await client.get("/api/limira/uploads")
        task_list_response = await client.get(
            "/api/limira/uploads",
            params={"task_id": "task-a"},
        )
        foreign_task_list_response = await client.get(
            "/api/limira/uploads",
            params={"task_id": "task-b"},
        )
        detail_response = await client.get(
            f"/api/limira/uploads/{linked_response.json()['document_id']}"
        )
        download_response = await client.get(
            f"/api/limira/uploads/{linked_response.json()['document_id']}/download"
        )
        foreign_detail_response = await client.get("/api/limira/uploads/foreign-doc")
        foreign_download_response = await client.get(
            "/api/limira/uploads/foreign-doc/download"
        )

    assert unlinked_response.status_code == 201
    assert linked_response.status_code == 201
    assert list_response.status_code == 200
    listed = list_response.json()["documents"]
    assert {document["filename"] for document in listed} == {
        "linked.txt",
        "unlinked.txt",
    }
    assert all("object_key" not in document for document in listed)
    assert all("minio_object_key" not in document for document in listed)
    assert all(document["download_url"].startswith("/api/limira/uploads/") for document in listed)
    _assert_no_browser_leak(listed)

    assert task_list_response.status_code == 200
    assert [document["filename"] for document in task_list_response.json()["documents"]] == [
        "linked.txt"
    ]
    assert foreign_task_list_response.status_code == 404

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["filename"] == "linked.txt"
    assert detail["task_id"] == "task-a"
    assert "object_key" not in detail
    assert "minio_object_key" not in detail
    _assert_no_browser_leak(detail)

    assert download_response.status_code == 200
    assert download_response.content == b"linked"
    assert download_response.headers["content-type"].startswith("text/plain")
    assert 'filename="linked.txt"' in download_response.headers["content-disposition"]
    assert download_response.headers["x-content-type-options"] == "nosniff"
    assert foreign_detail_response.status_code == 404
    assert foreign_download_response.status_code == 404


@pytest.mark.asyncio
async def test_upload_download_clears_missing_persisted_document_object():
    app, repo, storage = _limira_asgi_app()
    expected_bytes = b"expected document bytes"
    object_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="uploads",
        filename="missing.txt",
        object_id="doc-missing",
    )
    document = repo.record_uploaded_document(
        document_id="doc-missing",
        owner_user_id="user-a",
        task_id=None,
        original_filename="missing.txt",
        content_type="text/plain",
        byte_size=len(expected_bytes),
        minio_bucket=storage.bucket,
        object_key=object_key,
        extracted_text="missing document",
        language=None,
        metadata={"sha256": hashlib.sha256(expected_bytes).hexdigest()},
        embedding=None,
    )

    assert document.public_dict()["download_url"].endswith("/doc-missing/download")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get("/api/limira/uploads/doc-missing/download")
        detail_response = await client.get("/api/limira/uploads/doc-missing")
        search_response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "missing"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "document_object_not_found"
    refreshed = repo.get_user_document("doc-missing", "user-a")
    assert refreshed is not None
    assert refreshed.metadata["download_unavailable"] == "document_object_missing"
    assert "sha256" not in refreshed.metadata
    assert refreshed.public_dict()["download_url"] is None
    assert detail_response.status_code == 200
    assert detail_response.json()["download_url"] is None
    assert search_response.status_code == 200
    assert search_response.json()["documents"][0]["download_url"] is None


@pytest.mark.asyncio
async def test_upload_download_clears_mismatched_persisted_document_object():
    app, repo, storage = _limira_asgi_app()
    expected_bytes = b"expected document bytes"
    stale_bytes = b"stale document bytes"
    object_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="uploads",
        filename="mismatch.txt",
        object_id="doc-mismatch",
    )
    await storage.put_object(
        object_key=object_key,
        data=stale_bytes,
        content_type="text/plain",
        metadata={"document_id": "doc-mismatch"},
    )
    repo.record_uploaded_document(
        document_id="doc-mismatch",
        owner_user_id="user-a",
        task_id=None,
        original_filename="mismatch.txt",
        content_type="text/plain",
        byte_size=len(expected_bytes),
        minio_bucket=storage.bucket,
        object_key=object_key,
        extracted_text="mismatch document",
        language=None,
        metadata={"sha256": hashlib.sha256(expected_bytes).hexdigest()},
        embedding=None,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get("/api/limira/uploads/doc-mismatch/download")
        detail_response = await client.get("/api/limira/uploads/doc-mismatch")

    assert response.status_code == 404
    assert response.content != stale_bytes
    refreshed = repo.get_user_document("doc-mismatch", "user-a")
    assert refreshed is not None
    assert refreshed.metadata["download_unavailable"] == "document_sha_mismatch"
    assert "sha256" not in refreshed.metadata
    assert refreshed.public_dict()["download_url"] is None
    assert detail_response.status_code == 200
    assert detail_response.json()["download_url"] is None


@pytest.mark.asyncio
async def test_upload_download_clears_missing_persisted_document_checksum():
    app, repo, storage = _limira_asgi_app()
    object_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="uploads",
        filename="missing-sha.txt",
        object_id="doc-missing-sha",
    )
    await storage.put_object(
        object_key=object_key,
        data=b"document bytes",
        content_type="text/plain",
        metadata={"document_id": "doc-missing-sha"},
    )
    document = repo.record_uploaded_document(
        document_id="doc-missing-sha",
        owner_user_id="user-a",
        task_id=None,
        original_filename="missing-sha.txt",
        content_type="text/plain",
        byte_size=len(b"document bytes"),
        minio_bucket=storage.bucket,
        object_key=object_key,
        extracted_text="missing sha document",
        language=None,
        metadata={"source": "legacy"},
        embedding=None,
    )

    assert document.public_dict()["download_url"] is None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get("/api/limira/uploads/doc-missing-sha/download")
        detail_response = await client.get("/api/limira/uploads/doc-missing-sha")

    assert response.status_code == 404
    refreshed = repo.get_user_document("doc-missing-sha", "user-a")
    assert refreshed is not None
    assert refreshed.metadata == {
        "source": "legacy",
        "download_unavailable": "document_sha_missing",
    }
    assert detail_response.status_code == 200
    assert detail_response.json()["download_url"] is None


@pytest.mark.asyncio
async def test_upload_download_clears_invalid_persisted_document_object_key():
    app, repo, _storage = _limira_asgi_app()
    expected_bytes = b"expected invalid-key document bytes"
    document = repo.record_uploaded_document(
        document_id="doc-invalid-key",
        owner_user_id="user-a",
        task_id=None,
        original_filename="invalid-key.txt",
        content_type="text/plain",
        byte_size=len(expected_bytes),
        minio_bucket="limira-artifacts",
        object_key="../invalid-key.txt",
        extracted_text="invalid key document",
        language=None,
        metadata={
            "source": "legacy",
            "sha256": hashlib.sha256(expected_bytes).hexdigest(),
        },
        embedding=None,
    )

    assert document.public_dict()["download_url"] is None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get("/api/limira/uploads/doc-invalid-key/download")
        detail_response = await client.get("/api/limira/uploads/doc-invalid-key")

    assert response.status_code == 404
    assert response.json()["detail"] == "document_object_not_found"
    refreshed = repo.get_user_document("doc-invalid-key", "user-a")
    assert refreshed is not None
    assert refreshed.metadata == {
        "source": "legacy",
        "download_unavailable": "invalid_document_object_key",
    }
    assert refreshed.public_dict()["download_url"] is None
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["download_url"] is None
    assert "object_key" not in detail


@pytest.mark.asyncio
async def test_upload_download_clears_malformed_persisted_document_checksum():
    app, repo, storage = _limira_asgi_app()
    object_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="uploads",
        filename="malformed-sha.txt",
        object_id="doc-malformed-sha",
    )
    await storage.put_object(
        object_key=object_key,
        data=b"document bytes",
        content_type="text/plain",
        metadata={"document_id": "doc-malformed-sha"},
    )
    document = repo.record_uploaded_document(
        document_id="doc-malformed-sha",
        owner_user_id="user-a",
        task_id=None,
        original_filename="malformed-sha.txt",
        content_type="text/plain",
        byte_size=len(b"document bytes"),
        minio_bucket=storage.bucket,
        object_key=object_key,
        extracted_text="malformed sha document",
        language=None,
        metadata={"source": "legacy", "sha256": "not-a-valid-sha"},
        embedding=None,
    )

    assert document.public_dict()["download_url"] is None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get("/api/limira/uploads/doc-malformed-sha/download")
        detail_response = await client.get("/api/limira/uploads/doc-malformed-sha")

    assert response.status_code == 404
    refreshed = repo.get_user_document("doc-malformed-sha", "user-a")
    assert refreshed is not None
    assert refreshed.metadata == {
        "source": "legacy",
        "download_unavailable": "document_sha_malformed",
    }
    assert refreshed.public_dict()["download_url"] is None
    assert detail_response.status_code == 200
    assert detail_response.json()["download_url"] is None


@pytest.mark.asyncio
async def test_upload_search_is_owner_scoped_task_filterable_and_browser_safe():
    app, repo, storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="owned lithium task",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    repo.create_task(
        task_id="task-b",
        owner_user_id="user-b",
        query="foreign lithium task",
        scenario=None,
        runner_task_id="runner-task-b",
    )
    repo.record_uploaded_document(
        document_id="doc-lithium",
        owner_user_id="user-a",
        task_id="task-a",
        original_filename="lithium-brief.txt",
        content_type="text/plain",
        byte_size=41,
        minio_bucket=storage.bucket,
        object_key="limira/users/user-a/tasks/task-a/uploads/doc-lithium.txt",
        extracted_text=(
            "Critical minerals update: lithium export controls and graphite risks."
        ),
        language=None,
        metadata={},
    )
    repo.record_uploaded_document(
        document_id="doc-copper",
        owner_user_id="user-a",
        task_id=None,
        original_filename="copper.txt",
        content_type="text/plain",
        byte_size=20,
        minio_bucket=storage.bucket,
        object_key="limira/users/user-a/uploads/doc-copper.txt",
        extracted_text="Copper inventory report.",
        language=None,
        metadata={},
    )
    repo.record_uploaded_document(
        document_id="doc-foreign",
        owner_user_id="user-b",
        task_id="task-b",
        original_filename="foreign-lithium.txt",
        content_type="text/plain",
        byte_size=18,
        minio_bucket=storage.bucket,
        object_key="limira/users/user-b/tasks/task-b/uploads/doc-foreign.txt",
        extracted_text="Lithium sanctions.",
        language=None,
        metadata={},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        search_response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "lithium graphite"},
        )
        task_search_response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "lithium", "task_id": "task-a"},
        )
        foreign_task_response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "lithium", "task_id": "task-b"},
        )
        blank_response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "   "},
        )

    assert search_response.status_code == 200
    payload = search_response.json()
    assert [document["document_id"] for document in payload["documents"]] == [
        "doc-lithium"
    ]
    result = payload["documents"][0]
    assert result["score"] > 0
    assert result["matched_terms"] == ["lithium", "graphite"]
    assert "graphite risks" in result["snippet"]
    assert "object_key" not in json.dumps(payload)
    assert "minio_object_key" not in json.dumps(payload)
    _assert_no_browser_leak(payload)

    assert task_search_response.status_code == 200
    assert [
        document["document_id"]
        for document in task_search_response.json()["documents"]
    ] == ["doc-lithium"]
    assert foreign_task_response.status_code == 404
    assert blank_response.status_code == 400
    assert blank_response.json()["detail"] == "search_query_required"


@pytest.mark.asyncio
async def test_upload_search_uses_configured_embedding_ranking_when_enabled():
    app, repo, storage = _limira_asgi_app()
    provider = FakeUploadEmbeddingProvider([1.0, 0.0])

    async def embedding_config_override():
        return limira.LimiraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=2,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limira.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limira.get_upload_embedding_provider] = (
        embedding_provider_override
    )
    repo.record_uploaded_document(
        document_id="doc-vector",
        owner_user_id="user-a",
        task_id=None,
        original_filename="near-vector.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket=storage.bucket,
        object_key="limira/users/user-a/uploads/doc-vector.txt",
        extracted_text="Nickel supply memorandum.",
        language=None,
        metadata={},
        embedding=[1.0, 0.0],
    )
    repo.record_uploaded_document(
        document_id="doc-lexical",
        owner_user_id="user-a",
        task_id=None,
        original_filename="lexical-lithium.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket=storage.bucket,
        object_key="limira/users/user-a/uploads/doc-lexical.txt",
        extracted_text="Lithium lithium lithium.",
        language=None,
        metadata={},
        embedding=[0.0, 1.0],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "lithium", "limit": 2},
        )

    assert response.status_code == 200
    payload = response.json()
    assert [document["document_id"] for document in payload["documents"]] == [
        "doc-vector",
        "doc-lexical",
    ]
    assert payload["documents"][0]["score"] > payload["documents"][1]["score"]
    assert "embedding" not in json.dumps(payload)
    assert "object_key" not in json.dumps(payload)
    assert provider.calls == [
        {
            "text": "lithium",
            "config": limira.LimiraUploadEmbeddingConfig(
                enabled=True,
                provider="fake",
                model="fake-model",
                dimensions=2,
            ),
        }
    ]
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_upload_search_falls_back_to_lexical_when_embedding_provider_fails():
    app, repo, storage = _limira_asgi_app()
    provider = FakeFailingUploadEmbeddingProvider(RuntimeError("provider unavailable"))

    async def embedding_config_override():
        return limira.LimiraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=2,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limira.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limira.get_upload_embedding_provider] = (
        embedding_provider_override
    )
    repo.record_uploaded_document(
        document_id="doc-lexical",
        owner_user_id="user-a",
        task_id=None,
        original_filename="lithium-fallback.txt",
        content_type="text/plain",
        byte_size=20,
        minio_bucket=storage.bucket,
        object_key="limira/users/user-a/uploads/doc-lexical.txt",
        extracted_text="Lithium supply memo.",
        language=None,
        metadata={},
        embedding=[0.0, 1.0],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "lithium"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert [document["document_id"] for document in payload["documents"]] == [
        "doc-lexical"
    ]
    assert payload["documents"][0]["matched_terms"] == ["lithium"]
    assert "Lithium supply memo" in payload["documents"][0]["snippet"]
    assert "embedding" not in json.dumps(payload)
    assert "object_key" not in json.dumps(payload)
    assert provider.calls == [
        {
            "text": "lithium",
            "config": limira.LimiraUploadEmbeddingConfig(
                enabled=True,
                provider="fake",
                model="fake-model",
                dimensions=2,
            ),
        }
    ]
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_postgres_upload_search_falls_back_to_lexical_when_embedding_provider_fails():
    app, _repo, _storage = _limira_asgi_app()
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    provider = FakeFailingUploadEmbeddingProvider(RuntimeError("provider unavailable"))

    async def task_repository_override():
        return repo

    async def embedding_config_override():
        return limira.LimiraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=2,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limira.get_task_repository] = task_repository_override
    app.dependency_overrides[limira.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limira.get_upload_embedding_provider] = (
        embedding_provider_override
    )
    for document_id, owner_user_id, filename, text in (
        ("doc-lexical", "user-a", "cobalt-fallback.txt", "Cobalt supply memo."),
        ("doc-unmatched", "user-a", "nickel.txt", "Nickel market note."),
        ("doc-foreign", "user-b", "foreign-cobalt.txt", "Cobalt foreign memo."),
    ):
        repo.record_uploaded_document(
            document_id=document_id,
            owner_user_id=owner_user_id,
            task_id=None,
            original_filename=filename,
            content_type="text/plain",
            byte_size=len(text),
            minio_bucket="limira-artifacts",
            object_key=f"limira/users/{owner_user_id}/uploads/{document_id}.txt",
            extracted_text=text,
            language=None,
            metadata={},
            embedding=[0.0, 1.0],
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "cobalt"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert [document["document_id"] for document in payload["documents"]] == [
        "doc-lexical"
    ]
    assert payload["documents"][0]["matched_terms"] == ["cobalt"]
    assert "Cobalt supply memo" in payload["documents"][0]["snippet"]
    assert engine.vector_search_calls == []
    assert provider.calls == [
        {
            "text": "cobalt",
            "config": limira.LimiraUploadEmbeddingConfig(
                enabled=True,
                provider="fake",
                model="fake-model",
                dimensions=2,
            ),
        }
    ]
    assert "embedding" not in json.dumps(payload)
    assert "object_key" not in json.dumps(payload)
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_upload_search_rejects_embedding_dimension_mismatch():
    app, repo, storage = _limira_asgi_app()
    provider = FakeUploadEmbeddingProvider([1.0])

    async def embedding_config_override():
        return limira.LimiraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=2,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limira.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limira.get_upload_embedding_provider] = (
        embedding_provider_override
    )
    repo.record_uploaded_document(
        document_id="doc-vector",
        owner_user_id="user-a",
        task_id=None,
        original_filename="near-vector.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket=storage.bucket,
        object_key="limira/users/user-a/uploads/doc-vector.txt",
        extracted_text="Nickel supply memorandum.",
        language=None,
        metadata={},
        embedding=[1.0, 0.0],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "nickel"},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "upload_search_embedding_dimension_mismatch"


@pytest.mark.asyncio
async def test_postgres_upload_search_rejects_embedding_dimension_mismatch_before_repository_search():
    app, _repo, _storage = _limira_asgi_app()
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    provider = FakeUploadEmbeddingProvider([1.0])

    async def task_repository_override():
        return repo

    async def embedding_config_override():
        return limira.LimiraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=2,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limira.get_task_repository] = task_repository_override
    app.dependency_overrides[limira.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limira.get_upload_embedding_provider] = (
        embedding_provider_override
    )
    repo.record_uploaded_document(
        document_id="doc-vector",
        owner_user_id="user-a",
        task_id=None,
        original_filename="near-vector.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket="limira-artifacts",
        object_key="limira/users/user-a/uploads/doc-vector.txt",
        extracted_text="Nickel supply memorandum.",
        language=None,
        metadata={},
        embedding=[1.0, 0.0],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/uploads/search",
            params={"query": "nickel"},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "upload_search_embedding_dimension_mismatch"
    assert engine.vector_search_calls == []
    assert provider.calls == [
        {
            "text": "nickel",
            "config": limira.LimiraUploadEmbeddingConfig(
                enabled=True,
                provider="fake",
                model="fake-model",
                dimensions=2,
            ),
        }
    ]
    assert repo.get_user_document("doc-vector", "user-a") is not None


@pytest.mark.asyncio
async def test_pdf_route_rejects_object_key_aliases_on_actual_http_surface():
    app, repo, _storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="report",
        scenario=None,
        runner_task_id="runner-task-a",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        assert set(PLAN_NAMED_OBJECT_KEY_ALIASES).issubset(
            limira.OBJECT_KEY_FORBIDDEN_FIELDS
        )
        aliases = sorted(
            set(limira.OBJECT_KEY_FORBIDDEN_FIELDS)
            | set(PLAN_NAMED_OBJECT_KEY_ALIASES)
        )
        for alias in aliases:
            query_response = await client.post(
                "/api/limira/tasks/task-a/reports/pdf",
                params={alias: "users/user-a/reports/evil.pdf"},
                json={},
            )
            assert query_response.status_code == 400, alias
            assert query_response.json()["detail"] == "object_key_server_generated"

            json_response = await client.post(
                "/api/limira/tasks/task-a/reports/pdf",
                json={alias: "users/user-a/reports/evil.pdf"},
            )
            assert json_response.status_code == 400, alias
            assert json_response.json()["detail"] == "object_key_server_generated"

        empty_query_response = await client.post(
            "/api/limira/tasks/task-a/reports/pdf",
            params={"object_key": ""},
            json={},
        )
        assert empty_query_response.status_code == 400
        assert empty_query_response.json()["detail"] == "object_key_server_generated"

        duplicate_query_response = await client.post(
            "/api/limira/tasks/task-a/reports/pdf?object_key=users/user-a/reports/evil.pdf&object_key=",
            json={},
        )
        assert duplicate_query_response.status_code == 400
        assert duplicate_query_response.json()["detail"] == "object_key_server_generated"

        empty_json_response = await client.post(
            "/api/limira/tasks/task-a/reports/pdf",
            json={"object_key": ""},
        )
        assert empty_json_response.status_code == 400
        assert empty_json_response.json()["detail"] == "object_key_server_generated"


@pytest.mark.asyncio
async def test_pdf_route_exports_report_to_storage_and_persists_metadata():
    app, repo, storage = _limira_asgi_app()
    pdf_exporter = app.state.test_pdf_exporter
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="report",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    repo.create_task(
        task_id="task-b",
        owner_user_id="user-b",
        query="foreign report",
        scenario=None,
        runner_task_id="runner-task-b",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/tasks/task-a/reports/pdf",
            json={
                "report_id": "report-a",
                "report_type": "final",
                "markdown": "Finding references [EVID-001]",
                "evidence_refs": ["EVID-001"],
            },
        )
        unsafe_html_response = await client.post(
            "/api/limira/tasks/task-a/reports/pdf",
            json={
                "report_id": "report-html",
                "markdown": "Finding references [EVID-001]",
                "html": (
                    '<img src=x onerror=alert(1)>'
                    '<svg onload=alert(2)></svg>'
                    '<iframe srcdoc="<script>bad()</script>"></iframe>'
                    '<meta http-equiv="refresh" content="0;url=https://example.test">'
                ),
                "evidence_refs": ["EVID-001"],
            },
        )
        foreign_response = await client.post(
            "/api/limira/tasks/task-b/reports/pdf",
            json={
                "report_id": "report-b",
                "markdown": "foreign",
                "evidence_refs": [],
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["report_id"] == "report-a"
    assert payload["task_id"] == "task-a"
    assert payload["report_type"] == "final"
    assert payload["evidence_refs"] == ["EVID-001"]
    assert payload["pdf_size_bytes"] == len(pdf_exporter.pdf_bytes)
    assert payload["pdf_sha256"]
    assert payload["pdf_url"] == "/api/limira/tasks/task-a/reports/report-a/pdf"
    assert "object_key" not in payload
    assert "pdf_object_key" not in payload
    _assert_no_browser_leak(payload)

    assert len(pdf_exporter.html_inputs) == 1
    rendered_html = pdf_exporter.html_inputs[0]
    assert 'data-evidence-ref="EVID-001"' in rendered_html
    assert "<script" not in rendered_html.lower()
    assert "javascript:" not in rendered_html.lower()
    assert "onclick" not in rendered_html.lower()
    assert "content-security-policy" in rendered_html.lower()
    assert unsafe_html_response.status_code == 400
    assert unsafe_html_response.json()["detail"] == "browser_report_html_not_allowed"

    report = repo.get_user_report(
        task_id="task-a",
        report_id="report-a",
        owner_user_id="user-a",
    )
    assert report is not None
    assert report.markdown == "Finding references [EVID-001]"
    assert report.html == rendered_html
    assert report.evidence_refs == ["EVID-001"]
    assert report.pdf_object_key in storage.objects
    stored = storage.objects[report.pdf_object_key]
    assert stored["data"] == pdf_exporter.pdf_bytes
    assert stored["content_type"] == "application/pdf"
    assert stored["metadata"]["report_id"] == "report-a"
    assert stored["metadata"]["task_id"] == "task-a"
    assert stored["metadata"]["owner_user_id"] == "user-a"
    assert "/tasks/task-a/reports/" in report.pdf_object_key
    assert foreign_response.status_code == 404

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        download_response = await client.get(payload["pdf_url"])
        foreign_download_response = await client.get(
            "/api/limira/tasks/task-b/reports/report-a/pdf"
        )

    assert download_response.status_code == 200
    assert download_response.content == pdf_exporter.pdf_bytes
    assert download_response.headers["content-type"].startswith("application/pdf")
    assert 'filename="report-a.pdf"' in download_response.headers["content-disposition"]
    assert download_response.headers["x-content-type-options"] == "nosniff"
    assert foreign_download_response.status_code == 404


@pytest.mark.asyncio
async def test_pdf_route_rejects_blank_exporter_output_before_storage():
    app, repo, storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-blank-exporter-pdf",
        owner_user_id="user-a",
        query="blank exporter",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    blank_pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj\n"
        b"<</Length 300>> stream\n"
        b"compressed paint state without text or fonts\n"
        b"endstream\n"
        b"endobj\n"
        b"2 0 obj\n"
        b"<</Type /Page /Contents 1 0 R>>\n"
        b"endobj\n"
        b"%%EOF"
    )

    class BlankPdfExporter:
        async def render_pdf(self, _html_content):
            return blank_pdf_bytes

    async def pdf_exporter_override():
        return BlankPdfExporter()

    app.dependency_overrides[limira.get_pdf_exporter] = pdf_exporter_override

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/tasks/task-blank-exporter-pdf/reports/pdf",
            json={
                "report_id": "blank-exporter",
                "markdown": "# Visible report\n\n正文存在，但 PDF 输出为空。",
                "evidence_refs": [],
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "pdf_export_failed"
    assert not storage.objects
    assert (
        repo.get_user_report(
            task_id="task-blank-exporter-pdf",
            report_id="blank-exporter",
            owner_user_id="user-a",
        )
        is None
    )


@pytest.mark.asyncio
async def test_pdf_route_unwraps_json_wrapped_markdown_and_rejects_empty_output():
    app, repo, _storage = _limira_asgi_app()
    pdf_exporter = app.state.test_pdf_exporter
    repo.create_task(
        task_id="task-json-report-pdf",
        owner_user_id="user-a",
        query="json report",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    report_body = "## 研究结论\n\nPDF 应包含正文。 [EVID-001]"
    wrapped_report = json.dumps(
        {
            "id": "REPORT-001",
            "title": "JSON wrapped report",
            "content": report_body,
        },
        ensure_ascii=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        empty_response = await client.post(
            "/api/limira/tasks/task-json-report-pdf/reports/pdf",
            json={
                "report_id": "empty-report",
                "markdown": json.dumps({"title": "No content"}, ensure_ascii=False),
                "evidence_refs": [],
            },
        )
        wrapped_response = await client.post(
            "/api/limira/tasks/task-json-report-pdf/reports/pdf",
            json={
                "report_id": "wrapped-report",
                "markdown": wrapped_report,
                "evidence_refs": ["EVID-001"],
            },
        )

    assert empty_response.status_code == 400
    assert empty_response.json()["detail"] == "empty_report_markdown"
    assert wrapped_response.status_code == 201
    assert len(pdf_exporter.html_inputs) == 1
    rendered_html = pdf_exporter.html_inputs[0]
    assert "PDF 应包含正文。" in rendered_html
    assert "JSON wrapped report" not in rendered_html
    assert '"content"' not in rendered_html
    report = repo.get_user_report(
        task_id="task-json-report-pdf",
        report_id="wrapped-report",
        owner_user_id="user-a",
    )
    assert report is not None
    assert report.markdown == report_body


@pytest.mark.asyncio
async def test_pdf_route_writes_debug_artifacts_when_debug_dir_is_configured(
    monkeypatch,
    tmp_path,
):
    debug_dir = tmp_path / "limira-pdf-debug"
    monkeypatch.setenv("LIMIRA_PDF_DEBUG_DIR", str(debug_dir))
    app, repo, _storage = _limira_asgi_app()
    pdf_exporter = app.state.test_pdf_exporter
    repo.create_task(
        task_id="task-debug-pdf",
        owner_user_id="user-a",
        query="debug pdf",
        scenario=None,
        runner_task_id="runner-task-a",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/tasks/task-debug-pdf/reports/pdf",
            json={
                "report_id": "debug-report",
                "markdown": "Debug PDF body [EVID-001]",
                "evidence_refs": ["EVID-001"],
            },
        )

    assert response.status_code == 201
    html_files = list(debug_dir.glob("*.html"))
    pdf_files = list(debug_dir.glob("*.pdf"))
    manifest_files = list(debug_dir.glob("*.json"))
    assert len(html_files) == 1
    assert len(pdf_files) == 1
    assert len(manifest_files) == 1
    assert "Debug PDF body" in html_files[0].read_text(encoding="utf-8")
    assert pdf_files[0].read_bytes() == pdf_exporter.pdf_bytes
    manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
    assert manifest["task_id"] == "task-debug-pdf"
    assert manifest["report_id"] == "debug-report"
    assert manifest["pdf_bytes"] == len(pdf_exporter.pdf_bytes)
    report = repo.get_user_report(
        task_id="task-debug-pdf",
        report_id="debug-report",
        owner_user_id="user-a",
    )
    assert report is not None
    assert report.metadata["pdf_debug"]["pdf_path"] == str(pdf_files[0])


@pytest.mark.asyncio
async def test_pdf_download_clears_invalid_persisted_pdf_object_key():
    app, repo, _storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-invalid-pdf-key",
        owner_user_id="user-a",
        query="invalid pdf key",
        scenario=None,
        runner_task_id="runner-invalid-pdf-key",
    )
    report = repo.record_generated_report(
        report_id="report-invalid-key",
        task_id="task-invalid-pdf-key",
        report_type="final",
        markdown="Cached report",
        html="<p>Cached report</p>",
        pdf_object_key="../bad.pdf",
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={
            "pdf_bucket": "limira",
            "pdf_sha256": "bad-sha",
            "pdf_size_bytes": 123,
            "source": "cache",
        },
    )

    public_report = report.public_dict()
    assert public_report["pdf_url"] is None
    assert public_report["pdf_sha256"] is None
    assert public_report["pdf_size_bytes"] is None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/tasks/task-invalid-pdf-key/reports/report-invalid-key/pdf"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "report_pdf_not_found"
    refreshed = repo.get_user_report(
        task_id="task-invalid-pdf-key",
        report_id="report-invalid-key",
        owner_user_id="user-a",
    )
    assert refreshed is not None
    assert refreshed.pdf_object_key is None
    public_refreshed = refreshed.public_dict()
    assert public_refreshed["pdf_url"] is None
    assert public_refreshed["pdf_sha256"] is None
    assert public_refreshed["pdf_size_bytes"] is None
    assert refreshed.metadata == {"source": "cache"}


@pytest.mark.asyncio
async def test_pdf_download_clears_missing_persisted_pdf_object():
    app, repo, _storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-missing-pdf",
        owner_user_id="user-a",
        query="missing pdf",
        scenario=None,
        runner_task_id="runner-missing-pdf",
    )
    expected_sha = hashlib.sha256(b"expected pdf bytes").hexdigest()
    pdf_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="reports",
        task_id="task-missing-pdf",
        filename="report-missing.pdf",
        object_id="report-missing",
    )
    report = repo.record_generated_report(
        report_id="report-missing",
        task_id="task-missing-pdf",
        report_type="final",
        markdown="Cached report",
        html="<p>Cached report</p>",
        pdf_object_key=pdf_key,
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={
            "pdf_bucket": "limira",
            "pdf_sha256": expected_sha,
            "pdf_size_bytes": 18,
            "source": "cache",
        },
    )

    assert report.public_dict()["pdf_url"].endswith("/report-missing/pdf")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/tasks/task-missing-pdf/reports/report-missing/pdf"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "report_pdf_not_found"
    refreshed = repo.get_user_report(
        task_id="task-missing-pdf",
        report_id="report-missing",
        owner_user_id="user-a",
    )
    assert refreshed is not None
    assert refreshed.pdf_object_key is None
    public_refreshed = refreshed.public_dict()
    assert public_refreshed["pdf_url"] is None
    assert public_refreshed["pdf_sha256"] is None
    assert public_refreshed["pdf_size_bytes"] is None
    assert refreshed.metadata == {"source": "cache"}


@pytest.mark.asyncio
async def test_pdf_download_clears_mismatched_persisted_pdf_object():
    app, repo, storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-mismatch-pdf",
        owner_user_id="user-a",
        query="mismatch pdf",
        scenario=None,
        runner_task_id="runner-mismatch-pdf",
    )
    expected_sha = hashlib.sha256(b"expected pdf bytes").hexdigest()
    mismatched_bytes = b"not the expected pdf bytes"
    pdf_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="reports",
        task_id="task-mismatch-pdf",
        filename="report-mismatch.pdf",
        object_id="report-mismatch",
    )
    await storage.put_object(
        object_key=pdf_key,
        data=mismatched_bytes,
        content_type="application/pdf",
        metadata={"report_id": "report-mismatch"},
    )
    repo.record_generated_report(
        report_id="report-mismatch",
        task_id="task-mismatch-pdf",
        report_type="final",
        markdown="Cached report",
        html="<p>Cached report</p>",
        pdf_object_key=pdf_key,
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={
            "pdf_bucket": "limira",
            "pdf_sha256": expected_sha,
            "pdf_size_bytes": len(b"expected pdf bytes"),
            "source": "cache",
        },
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/tasks/task-mismatch-pdf/reports/report-mismatch/pdf"
        )

    assert response.status_code == 404
    assert response.content != mismatched_bytes
    refreshed = repo.get_user_report(
        task_id="task-mismatch-pdf",
        report_id="report-mismatch",
        owner_user_id="user-a",
    )
    assert refreshed is not None
    assert refreshed.pdf_object_key is None
    assert refreshed.public_dict()["pdf_url"] is None
    assert refreshed.metadata == {"source": "cache"}


@pytest.mark.asyncio
async def test_pdf_download_clears_missing_persisted_pdf_checksum():
    app, repo, storage = _limira_asgi_app()
    repo.create_task(
        task_id="task-missing-pdf-sha",
        owner_user_id="user-a",
        query="missing pdf sha",
        scenario=None,
        runner_task_id="runner-missing-pdf-sha",
    )
    pdf_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="reports",
        task_id="task-missing-pdf-sha",
        filename="report-missing-sha.pdf",
        object_id="report-missing-sha",
    )
    await storage.put_object(
        object_key=pdf_key,
        data=b"%PDF-1.7\nmissing sha\n%%EOF",
        content_type="application/pdf",
        metadata={"report_id": "report-missing-sha"},
    )
    report = repo.record_generated_report(
        report_id="report-missing-sha",
        task_id="task-missing-pdf-sha",
        report_type="final",
        markdown="Cached report",
        html="<p>Cached report</p>",
        pdf_object_key=pdf_key,
        evidence_refs=[],
        creator_user_id="user-a",
        metadata={"source": "cache"},
    )

    assert report.public_dict()["pdf_url"] is None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.get(
            "/api/limira/tasks/task-missing-pdf-sha/reports/report-missing-sha/pdf"
        )

    assert response.status_code == 404
    refreshed = repo.get_user_report(
        task_id="task-missing-pdf-sha",
        report_id="report-missing-sha",
        owner_user_id="user-a",
    )
    assert refreshed is not None
    assert refreshed.pdf_object_key is None
    assert refreshed.metadata == {"source": "cache"}


def test_report_html_renderer_strips_active_markup_from_markdown():
    rendered_html = limira._render_report_html(
        markdown=(
            "# Final Report\n\n"
            "Finding **important** [EVID-001]\n\n"
            "| Item | Status |\n"
            "| --- | --- |\n"
            "| BYD | not listed |\n\n"
            "- Verify official list\n"
            "- Monitor updates\n\n"
            '<img src="https://example.test/leak?token=RUNNER_SERVICE_TOKEN" onerror=alert(1)>\n'
            "<svg onload=alert(2)><text>bad</text></svg>\n"
            '<iframe srcdoc="<script>bad()</script>"></iframe>\n'
            '<meta http-equiv="refresh" content="0;url=https://example.test">\n'
            '<a href="javascript:alert(3)">bad link</a>\n'
            '<a href="data:text/html,bad">data link</a>\n'
            '<a href="blob:https://example.test/bad">blob link</a>\n'
            '<a href="file:///etc/passwd">file link</a>'
        ),
        evidence_refs=["EVID-001"],
    )

    lower_html = rendered_html.lower()
    assert "<h1>Final Report</h1>" in rendered_html
    assert "<strong>important</strong>" in rendered_html
    assert "<table>" in rendered_html
    assert "<th>Item</th>" in rendered_html
    assert "<td>BYD</td>" in rendered_html
    assert "<ul>" in rendered_html
    assert "<li>Verify official list</li>" in rendered_html
    assert 'data-evidence-ref="EVID-001"' in rendered_html
    assert "content-security-policy" in lower_html
    for forbidden in [
        "<img",
        "onerror",
        "<svg",
        "onload",
        "<iframe",
        "srcdoc",
        "refresh",
        "<script",
        "javascript:",
        "data:",
        "blob:",
        "file:",
        "https://example.test",
        "runner_service_token",
    ]:
        assert forbidden not in lower_html


@pytest.mark.asyncio
async def test_pdf_route_scrubs_report_secrets_before_persistence_and_export():
    app, repo, storage = _limira_asgi_app()
    pdf_exporter = app.state.test_pdf_exporter
    repo.create_task(
        task_id="task-secret-report",
        owner_user_id="user-a",
        query="report secrets",
        scenario=None,
        runner_task_id="runner-secret-report",
    )

    markdown = (
        "Finding [EVID-001]\n\n"
        "Authorization: Bearer report-bearer-secret-123456\n"
        "OPENAI_API_KEY=sk-reportopenai123456\n"
        "SERPER_API_KEY=serper-secret-123456\n"
        "Cookie: session=report-cookie-secret-123456\n"
        "https://example.test/path?token=report-url-token-123456"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        response = await client.post(
            "/api/limira/tasks/task-secret-report/reports/pdf",
            json={
                "report_id": "report-secret",
                "report_type": "OPENAI_API_KEY=sk-reportopenai123456",
                "markdown": markdown,
                "evidence_refs": ["EVID-001", "JINA_API_KEY=nested-jina-secret-123456"],
            },
        )

    assert response.status_code == 201
    payload = response.json()
    _assert_no_raw_secret(payload)
    report = repo.get_user_report(
        task_id="task-secret-report",
        report_id="report-secret",
        owner_user_id="user-a",
    )
    assert report is not None
    assert limira.LIMIRA_SECRET_REDACTION in report.markdown
    assert limira.LIMIRA_SECRET_REDACTION in report.html
    assert "REDACTED" in report.report_type
    assert limira.LIMIRA_SECRET_REDACTION in report.evidence_refs
    assert report.pdf_object_key in storage.objects
    stored = storage.objects[report.pdf_object_key]
    _assert_no_raw_secret(report.markdown)
    _assert_no_raw_secret(report.html)
    _assert_no_raw_secret(report.metadata)
    _assert_no_raw_secret(report.evidence_refs)
    _assert_no_raw_secret(stored["metadata"])
    assert len(pdf_exporter.html_inputs) == 1
    _assert_no_raw_secret(pdf_exporter.html_inputs[0])


def test_limira_secret_scrubber_redacts_nested_payloads_and_urls():
    news_url = (
        "https://www.scmp.com/news/china/diplomacy/article/3356419/"
        "us-adds-alibaba-byd-and-other-chinese-tech-champions-military-company-list"
    )
    payload = {
        "headers": {
            "Authorization": "Bearer report-bearer-secret-123456",
            "Cookie": "session=report-cookie-secret-123456",
            "OPENAI_API_KEY=sk-headerkeysecret123456": "header key secret",
        },
        "nested": [
            "OPENAI_API_KEY=sk-reportopenai123456",
            {
                "news_url": news_url,
                "news_result": json.dumps({"title": "SCMP result", "url": news_url}),
                "safe": "https://example.test/path?token=report-url-token-123456",
                "userinfo_url": (
                    "https://user:password-secret-123456@example.test/path?topic=byd"
                ),
                "jina_api_key": "nested-jina-secret-123456",
                "Authorization: Bearer nested-key-secret-123456": "nested key secret",
            },
        ],
        "jwt": "eyJtrace.secret.payload",
    }

    scrubbed = limira.scrub_limira_secrets(payload)

    _assert_no_raw_secret(scrubbed)
    text = json.dumps(scrubbed, ensure_ascii=False)
    assert text.count(limira.LIMIRA_SECRET_REDACTION) >= 5
    assert "Authorization" in scrubbed["headers"]
    assert scrubbed["headers"]["Authorization"] == limira.LIMIRA_SECRET_REDACTION
    serialized = json.dumps(scrubbed, ensure_ascii=False)
    assert "sk-headerkeysecret123456" not in serialized
    assert "nested-key-secret-123456" not in serialized
    assert "password-secret-123456" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "Authorization: Bearer" not in serialized
    assert scrubbed["nested"][1]["news_url"] == news_url
    assert news_url in scrubbed["nested"][1]["news_result"]
    assert scrubbed["nested"][1]["userinfo_url"] == (
        "https://example.test/path?topic=byd"
    )
    assert scrubbed["nested"][1]["safe"].startswith("https://example.test/path?")
    assert "report-url-token-123456" not in scrubbed["nested"][1]["safe"]
    assert "topic=byd" in scrubbed["nested"][1]["userinfo_url"]
    assert limira.LIMIRA_SECRET_REDACTION in scrubbed["headers"]
    assert any(
        str(key).startswith(limira.LIMIRA_SECRET_REDACTION)
        for key in scrubbed["nested"][1]
    )


def test_playwright_pdf_exporter_builds_local_runtime_launch_env(monkeypatch, tmp_path):
    runtime_path = tmp_path / "playwright-runtime"
    usr_lib = runtime_path / "usr/lib/x86_64-linux-gnu"
    lib = runtime_path / "lib/x86_64-linux-gnu"
    runtime_fonts = runtime_path / "usr/share/fonts"
    usr_lib.mkdir(parents=True)
    lib.mkdir(parents=True)
    runtime_fonts.mkdir(parents=True)
    fonts_conf = runtime_path / "fonts.conf"
    fonts_conf.write_text(
        "<fontconfig><dir>/stale/renamed/project/fonts</dir></fontconfig>",
        encoding="utf-8",
    )

    monkeypatch.setenv(limira.LIMIRA_PLAYWRIGHT_RUNTIME_PATH_ENV, str(runtime_path))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/existing/lib")
    monkeypatch.delenv("FONTCONFIG_FILE", raising=False)
    monkeypatch.delenv("FONTCONFIG_PATH", raising=False)

    launch_env = limira._playwright_chromium_launch_env()

    assert launch_env is not None
    assert launch_env["LD_LIBRARY_PATH"].split(os.pathsep) == [
        str(usr_lib),
        str(lib),
        "/existing/lib",
    ]
    generated_fonts_conf = Path(launch_env["FONTCONFIG_FILE"])
    assert generated_fonts_conf == runtime_path / "limira-fonts.conf"
    generated_fonts_conf_text = generated_fonts_conf.read_text(encoding="utf-8")
    assert str(runtime_fonts) in generated_fonts_conf_text
    assert "/stale/renamed/project/fonts" not in generated_fonts_conf_text
    assert launch_env["FONTCONFIG_PATH"] == str(runtime_path)


@pytest.mark.asyncio
async def test_playwright_pdf_exporter_blocks_browser_resource_requests(monkeypatch):
    calls = {"launch_args": None, "launch_env": None, "closed": False}

    class FakeRoute:
        def __init__(self):
            self.aborted = False

        async def abort(self):
            self.aborted = True

    class FakePage:
        def __init__(self, visible_text="report"):
            self.routes = []
            self.set_content_calls = []
            self.evaluate_calls = []
            self.emulate_media_calls = []
            self.pdf_calls = []
            self.visible_text = visible_text

        async def route(self, pattern, handler):
            self.routes.append((pattern, handler))

        async def set_content(self, html_content, wait_until):
            self.set_content_calls.append(
                {"html_content": html_content, "wait_until": wait_until}
            )

        async def evaluate(self, expression):
            self.evaluate_calls.append(expression)
            return self.visible_text

        async def emulate_media(self, **kwargs):
            self.emulate_media_calls.append(kwargs)

        async def pdf(self, **kwargs):
            self.pdf_calls.append(kwargs)
            return b"%PDF-1.7\nfake\n%%EOF"

    class FakeBrowser:
        def __init__(self, page):
            self.page = page

        async def new_page(self):
            return self.page

        async def close(self):
            calls["closed"] = True

    class FakeChromium:
        def __init__(self, browser):
            self.browser = browser

        async def launch(self, **kwargs):
            calls["launch_args"] = kwargs["args"]
            calls["launch_env"] = kwargs.get("env")
            return self.browser

    class FakePlaywrightContext:
        def __init__(self, chromium):
            self.chromium = chromium

        async def __aenter__(self):
            return types.SimpleNamespace(chromium=self.chromium)

        async def __aexit__(self, exc_type, exc, tb):
            return False

    page = FakePage()
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)
    async_api_module = types.ModuleType("playwright.async_api")
    async_api_module.async_playwright = lambda: FakePlaywrightContext(chromium)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)

    pdf_bytes = await limira.PlaywrightLimiraPdfExporter().render_pdf(
        "<!doctype html><html><body>report</body></html>"
    )

    assert pdf_bytes.startswith(b"%PDF")
    assert calls["launch_args"] == ["--no-sandbox"]
    if calls["launch_env"] is not None:
        assert calls["launch_env"]["LD_LIBRARY_PATH"]
    assert calls["closed"] is True
    assert page.set_content_calls == [
        {
            "html_content": "<!doctype html><html><body>report</body></html>",
            "wait_until": "load",
        }
    ]
    assert page.evaluate_calls == ["() => document.body ? document.body.textContent : ''"]
    assert page.emulate_media_calls == [{"media": "print"}]
    assert page.pdf_calls == [{"format": "A4", "print_background": True}]
    assert len(page.routes) == 1
    pattern, handler = page.routes[0]
    assert pattern == "**/*"
    route = FakeRoute()
    await handler(route)
    assert route.aborted is True


@pytest.mark.asyncio
async def test_playwright_pdf_exporter_rejects_blank_rendered_body(monkeypatch):
    calls = {"closed": False}

    class FakePage:
        def __init__(self):
            self.pdf_called = False

        async def route(self, _pattern, _handler):
            return None

        async def set_content(self, _html_content, **_kwargs):
            return None

        async def evaluate(self, _expression):
            return "   "

        async def emulate_media(self, **_kwargs):
            return None

        async def pdf(self, **_kwargs):
            self.pdf_called = True
            return b"%PDF-1.7\nfake\n%%EOF"

    class FakeBrowser:
        def __init__(self, page):
            self.page = page

        async def new_page(self):
            return self.page

        async def close(self):
            calls["closed"] = True

    class FakeChromium:
        def __init__(self, browser):
            self.browser = browser

        async def launch(self, **_kwargs):
            return self.browser

    class FakePlaywrightContext:
        def __init__(self, chromium):
            self.chromium = chromium

        async def __aenter__(self):
            return types.SimpleNamespace(chromium=self.chromium)

        async def __aexit__(self, exc_type, exc, tb):
            return False

    page = FakePage()
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)
    async_api_module = types.ModuleType("playwright.async_api")
    async_api_module.async_playwright = lambda: FakePlaywrightContext(chromium)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)

    with pytest.raises(RuntimeError, match="limira_pdf_blank_rendered_body"):
        await limira.PlaywrightLimiraPdfExporter().render_pdf(
            "<!doctype html><html><body></body></html>"
        )

    assert page.pdf_called is False
    assert calls["closed"] is True


@pytest.mark.asyncio
async def test_playwright_pdf_exporter_rejects_blank_rendered_pdf(monkeypatch):
    calls = {"closed": False}
    blank_pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj\n"
        b"<</Length 300>> stream\n"
        b"compressed paint state without text or fonts\n"
        b"endstream\n"
        b"endobj\n"
        b"2 0 obj\n"
        b"<</Type /Page /Contents 1 0 R>>\n"
        b"endobj\n"
        b"%%EOF"
    )

    class FakePage:
        async def route(self, _pattern, _handler):
            return None

        async def set_content(self, _html_content, **_kwargs):
            return None

        async def evaluate(self, _expression):
            return "Visible report text"

        async def emulate_media(self, **_kwargs):
            return None

        async def pdf(self, **_kwargs):
            return blank_pdf_bytes

    class FakeBrowser:
        async def new_page(self):
            return FakePage()

        async def close(self):
            calls["closed"] = True

    class FakeChromium:
        async def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywrightContext:
        async def __aenter__(self):
            return types.SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async_api_module = types.ModuleType("playwright.async_api")
    async_api_module.async_playwright = lambda: FakePlaywrightContext()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)

    with pytest.raises(RuntimeError, match="limira_pdf_blank_rendered_pdf"):
        await limira.PlaywrightLimiraPdfExporter().render_pdf(
            "<!doctype html><html><body><h1>Visible report text</h1><p>中文正文</p></body></html>"
        )

    assert calls["closed"] is True


@pytest.mark.asyncio
async def test_playwright_pdf_exporter_raises_when_browser_launch_fails(monkeypatch):
    class FakeChromium:
        async def launch(self, **_kwargs):
            raise RuntimeError("missing browser deps")

    class FakePlaywrightContext:
        async def __aenter__(self):
            return types.SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async_api_module = types.ModuleType("playwright.async_api")
    async_api_module.async_playwright = lambda: FakePlaywrightContext()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)

    with pytest.raises(RuntimeError, match="missing browser deps"):
        await limira.PlaywrightLimiraPdfExporter().render_pdf(
            "<!doctype html><html><body><h1>Report title</h1><p>中文正文</p></body></html>"
        )


def test_postgres_repository_sql_targets_limira_task_and_artifact_tables():
    sql = limira.PostgresLimiraTaskRepository.sql_contract().lower()

    for table in (
        "limira_research_tasks",
        "limira_artifact_events",
        "limira_artifact_trace_events",
        "limira_task_event_logs",
        "limira_evidence_items",
        "limira_entities",
        "limira_entity_relations",
        "limira_timeline_events",
        "limira_generated_reports",
        "limira_uploaded_documents",
    ):
        assert table in sql

    for artifact_type in (
        "evidence",
        "entity",
        "relation",
        "timeline_event",
        "map_feature",
        "verification",
        "report_section",
    ):
        assert artifact_type in limira.ARTIFACT_BUCKETS

    assert "select artifact_type, payload" in sql
    assert "insert into limira_artifact_trace_events" in sql
    assert "from limira_artifact_trace_events" in sql
    assert "insert into limira_task_event_logs" in sql
    assert "from limira_task_event_logs" in sql
    assert "on conflict" in sql
    assert "on conflict (task_id, artifact_type, local_artifact_id)" in sql
    assert "on conflict (task_id, evidence_id)" in sql
    assert "on conflict (task_id, entity_id)" in sql
    assert "on conflict (task_id, relation_id)" in sql
    assert "on conflict (task_id, timeline_event_id)" in sql
    assert "on conflict (task_id, report_id)" in sql
    assert "on conflict (artifact_id)" not in sql
    assert "on conflict (evidence_id)" not in sql
    assert "cast(:published_at as timestamptz)" in sql
    assert "cast(:event_time as timestamptz)" in sql
    assert "cast(:event_time_end as timestamptz)" in sql
    assert "st_geomfromgeojson(:geometry_geojson)" in sql
    assert "st_geomfromtext(:geometry_wkt)" in sql
    assert "pdf_object_key" in sql
    assert "from limira_generated_reports" in sql
    assert "order by created_at desc" in sql
    assert "returning" in sql
    assert "insert into limira_uploaded_documents" in sql
    assert "object_key" in sql
    assert "extracted_text" in sql
    assert "embedding" in sql
    assert "cast(:embedding as vector)" in sql
    assert "embedding <=> cast(:query_embedding as vector)" in sql
    assert "embedding is not null" in sql
    assert "archive_object_key" in sql
    assert "archive_zip_sha256" in sql


def test_postgres_repository_preserves_task_local_artifact_refs_by_task():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    for task_id, owner in (("task-a", "user-a"), ("task-b", "user-b")):
        repo.create_task(
            task_id=task_id,
            owner_user_id=owner,
            query=f"{task_id} query",
            scenario=None,
            runner_task_id=f"runner-{task_id}",
        )

    for task_id, suffix in (("task-a", "A"), ("task-b", "B")):
        repo.record_artifact(
            task_id,
            "evidence",
            {"evidence_id": "EVID-001", "title": f"Evidence {suffix}"},
        )
        repo.record_artifact(
            task_id,
            "entity",
            {
                "entity_id": "ENT-001",
                "entity_type": "country",
                "display_name": f"Entity {suffix}",
            },
        )
        repo.record_artifact(
            task_id,
            "relation",
            {"relation_id": "REL-001", "relation_type": "mentions"},
        )
        repo.record_artifact(
            task_id,
            "report_section",
            {"section_id": "REPORT-001", "markdown": f"Report {suffix}"},
        )

    task_a_artifacts = repo.get_artifacts("task-a")
    task_b_artifacts = repo.get_artifacts("task-b")

    assert task_a_artifacts["evidence"][0]["title"] == "Evidence A"
    assert task_b_artifacts["evidence"][0]["title"] == "Evidence B"
    assert task_a_artifacts["entities"][0]["display_name"] == "Entity A"
    assert task_b_artifacts["entities"][0]["display_name"] == "Entity B"
    assert task_a_artifacts["report_sections"][0]["markdown"] == "Report A"
    assert task_b_artifacts["report_sections"][0]["markdown"] == "Report B"
    assert ("task-a", "evidence", "EVID-001") in engine.artifact_events
    assert ("task-b", "evidence", "EVID-001") in engine.artifact_events


def test_postgres_repository_records_artifact_trace_events_and_warnings():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-trace",
        owner_user_id="user-a",
        query="trace artifact events",
        scenario=None,
        runner_task_id="runner-trace",
    )

    repo.record_artifact(
        "task-trace",
        "evidence",
        {
            "evidence_id": "EVID-001",
            "title": "Evidence A",
            "source_event_type": "record_research_artifact",
        },
    )
    repo.record_artifact_trace_event(
        "task-trace",
        {
            "type": "artifact_warning",
            "artifact_type": "relation",
            "payload": {
                "warning": "invalid_artifact_payload",
                "artifact_type": "relation",
                "source_event_type": "record_research_artifact",
            },
            "source_event_type": "record_research_artifact",
        },
    )

    trace_events = repo.get_artifact_trace_events("task-trace")

    assert [event["type"] for event in trace_events] == [
        "evidence_collected",
        "artifact_warning",
    ]
    assert trace_events[0]["artifact_type"] == "evidence"
    assert trace_events[0]["bucket"] == "evidence"
    assert trace_events[0]["local_artifact_id"] == "EVID-001"
    assert trace_events[0]["source_event_type"] == "record_research_artifact"
    assert trace_events[0]["payload"]["title"] == "Evidence A"
    assert trace_events[1]["artifact_type"] == "relation"
    assert trace_events[1]["payload"]["warning"] == "invalid_artifact_payload"
    assert engine.artifact_trace_events[0]["task_id"] == "task-trace"


def test_postgres_repository_artifact_params_include_temporal_and_geometry_fields():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-geo",
        owner_user_id="user-a",
        query="geo query",
        scenario=None,
        runner_task_id="runner-geo",
    )
    repo.record_artifact(
        "task-geo",
        "evidence",
        {
            "evidence_id": "EVID-001",
            "title": "dated source",
            "published_at": "2026-01-02T03:04:05Z",
        },
    )
    repo.record_artifact(
        "task-geo",
        "timeline_event",
        {
            "event_id": "TIME-001",
            "title": "port disruption",
            "event_time": "2026-02-01T00:00:00Z",
            "event_time_end": "2026-02-02T00:00:00Z",
            "geometry": {"type": "Point", "coordinates": [32.55, 29.97]},
        },
    )
    repo.record_artifact(
        "task-geo",
        "map_feature",
        {
            "feature_id": "MAP-001",
            "title": "shipping lane",
            "geometry": "LINESTRING(32.5 29.9, 33.0 30.1)",
        },
    )
    repo.record_artifact(
        "task-geo",
        "entity",
        {
            "entity_id": "ENT-001",
            "entity_type": "location",
            "display_name": "Suez Canal",
            "lat": 29.97,
            "lon": 32.55,
        },
    )

    evidence_params = engine.typed_inserts["limira_evidence_items"][0]
    timeline_params = engine.typed_inserts["limira_timeline_events"][0]
    map_params = engine.typed_inserts["limira_timeline_events"][1]
    entity_params = engine.typed_inserts["limira_entities"][0]

    assert evidence_params["published_at"] == "2026-01-02T03:04:05Z"
    assert timeline_params["event_time"] == "2026-02-01T00:00:00Z"
    assert timeline_params["event_time_end"] == "2026-02-02T00:00:00Z"
    assert json.loads(timeline_params["geometry_geojson"]) == {
        "type": "Point",
        "coordinates": [32.55, 29.97],
    }
    assert map_params["geometry_wkt"] == "LINESTRING(32.5 29.9, 33.0 30.1)"
    assert json.loads(entity_params["geometry_geojson"]) == {
        "type": "Point",
        "coordinates": [32.55, 29.97],
    }


def test_postgres_repository_records_uploaded_documents_to_task_scoped_table():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-doc",
        owner_user_id="user-a",
        query="document query",
        scenario=None,
        runner_task_id="runner-doc",
    )

    document = repo.record_uploaded_document(
        document_id="doc-001",
        owner_user_id="user-a",
        task_id="task-doc",
        original_filename="brief.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket="limira-artifacts",
        object_key="limira/users/hash/tasks/task-doc/uploads/doc-001.txt",
        extracted_text="brief text",
        language=None,
        metadata={"sha256": "abc123"},
        embedding=[0.1, 0.2, 0.3],
    )

    assert document.document_id == "doc-001"
    assert document.task_id == "task-doc"
    assert document.owner_user_id == "user-a"
    assert document.object_key.endswith("/uploads/doc-001.txt")
    assert document.extracted_text == "brief text"
    assert document.embedding == [0.1, 0.2, 0.3]
    assert document.metadata == {"sha256": "abc123"}
    assert engine.uploaded_documents["doc-001"]["minio_bucket"] == "limira-artifacts"
    assert engine.uploaded_documents["doc-001"]["embedding"] == "[0.1,0.2,0.3]"

    owned = repo.get_user_document("doc-001", "user-a")
    assert owned is not None
    assert owned.original_filename == "brief.txt"
    assert owned.embedding == [0.1, 0.2, 0.3]
    assert repo.get_user_document("doc-001", "user-b") is None
    assert [document.document_id for document in repo.list_user_documents(owner_user_id="user-a")] == [
        "doc-001"
    ]
    assert [
        document.document_id
        for document in repo.list_user_documents(owner_user_id="user-a", task_id="task-doc")
    ] == ["doc-001"]
    assert repo.list_user_documents(owner_user_id="user-a", task_id="task-other") == []
    assert repo.list_user_documents(owner_user_id="user-b") == []


@pytest.mark.asyncio
async def test_postgres_upload_download_clears_missing_persisted_document_object():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    expected_bytes = b"expected postgres document bytes"
    object_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="uploads",
        filename="missing-postgres.txt",
        object_id="doc-postgres-missing",
    )
    repo.record_uploaded_document(
        document_id="doc-postgres-missing",
        owner_user_id="user-a",
        task_id=None,
        original_filename="missing-postgres.txt",
        content_type="text/plain",
        byte_size=len(expected_bytes),
        minio_bucket=storage.bucket,
        object_key=object_key,
        extracted_text="missing postgres document",
        language=None,
        metadata={"sha256": hashlib.sha256(expected_bytes).hexdigest()},
        embedding=None,
    )

    with pytest.raises(HTTPException) as exc:
        await limira.download_uploaded_document(
            "doc-postgres-missing",
            user=limira.LimiraUser("user-a"),
            repo=repo,
            object_storage=storage,
        )

    assert exc.value.status_code == 404
    refreshed = repo.get_user_document("doc-postgres-missing", "user-a")
    assert refreshed is not None
    assert refreshed.public_dict()["download_url"] is None
    assert refreshed.metadata["download_unavailable"] == "document_object_missing"
    assert "sha256" not in refreshed.metadata
    persisted = json.loads(engine.uploaded_documents["doc-postgres-missing"]["metadata"])
    assert persisted["download_unavailable"] == "document_object_missing"
    assert "sha256" not in persisted


@pytest.mark.asyncio
async def test_postgres_upload_download_clears_mismatched_persisted_document_object():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    expected_bytes = b"expected postgres document bytes"
    stale_bytes = b"stale postgres document bytes"
    object_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="uploads",
        filename="mismatch-postgres.txt",
        object_id="doc-postgres-mismatch",
    )
    await storage.put_object(
        object_key=object_key,
        data=stale_bytes,
        content_type="text/plain",
        metadata={"document_id": "doc-postgres-mismatch"},
    )
    repo.record_uploaded_document(
        document_id="doc-postgres-mismatch",
        owner_user_id="user-a",
        task_id=None,
        original_filename="mismatch-postgres.txt",
        content_type="text/plain",
        byte_size=len(expected_bytes),
        minio_bucket=storage.bucket,
        object_key=object_key,
        extracted_text="mismatch postgres document",
        language=None,
        metadata={"sha256": hashlib.sha256(expected_bytes).hexdigest()},
        embedding=None,
    )

    with pytest.raises(HTTPException) as exc:
        await limira.download_uploaded_document(
            "doc-postgres-mismatch",
            user=limira.LimiraUser("user-a"),
            repo=repo,
            object_storage=storage,
        )

    assert exc.value.status_code == 404
    refreshed = repo.get_user_document("doc-postgres-mismatch", "user-a")
    assert refreshed is not None
    assert refreshed.public_dict()["download_url"] is None
    assert refreshed.metadata["download_unavailable"] == "document_sha_mismatch"
    assert "sha256" not in refreshed.metadata
    persisted = json.loads(engine.uploaded_documents["doc-postgres-mismatch"]["metadata"])
    assert persisted["download_unavailable"] == "document_sha_mismatch"
    assert "sha256" not in persisted


@pytest.mark.asyncio
async def test_postgres_upload_download_clears_invalid_persisted_document_object_key():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    expected_bytes = b"expected postgres invalid-key document bytes"
    repo.record_uploaded_document(
        document_id="doc-postgres-invalid-key",
        owner_user_id="user-a",
        task_id=None,
        original_filename="invalid-postgres.txt",
        content_type="text/plain",
        byte_size=len(expected_bytes),
        minio_bucket=storage.bucket,
        object_key="../invalid-postgres.txt",
        extracted_text="invalid postgres document",
        language=None,
        metadata={
            "source": "legacy",
            "sha256": hashlib.sha256(expected_bytes).hexdigest(),
        },
        embedding=None,
    )

    with pytest.raises(HTTPException) as exc:
        await limira.download_uploaded_document(
            "doc-postgres-invalid-key",
            user=limira.LimiraUser("user-a"),
            repo=repo,
            object_storage=storage,
        )

    assert exc.value.status_code == 404
    refreshed = repo.get_user_document("doc-postgres-invalid-key", "user-a")
    assert refreshed is not None
    assert refreshed.public_dict()["download_url"] is None
    assert refreshed.metadata == {
        "source": "legacy",
        "download_unavailable": "invalid_document_object_key",
    }
    persisted = json.loads(engine.uploaded_documents["doc-postgres-invalid-key"]["metadata"])
    assert persisted == {
        "source": "legacy",
        "download_unavailable": "invalid_document_object_key",
    }


@pytest.mark.asyncio
async def test_postgres_upload_download_clears_malformed_persisted_document_checksum():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    object_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="uploads",
        filename="malformed-postgres.txt",
        object_id="doc-postgres-malformed-sha",
    )
    await storage.put_object(
        object_key=object_key,
        data=b"postgres document bytes",
        content_type="text/plain",
        metadata={"document_id": "doc-postgres-malformed-sha"},
    )
    repo.record_uploaded_document(
        document_id="doc-postgres-malformed-sha",
        owner_user_id="user-a",
        task_id=None,
        original_filename="malformed-postgres.txt",
        content_type="text/plain",
        byte_size=len(b"postgres document bytes"),
        minio_bucket=storage.bucket,
        object_key=object_key,
        extracted_text="malformed postgres document",
        language=None,
        metadata={"source": "legacy", "sha256": "not-a-valid-sha"},
        embedding=None,
    )

    with pytest.raises(HTTPException) as exc:
        await limira.download_uploaded_document(
            "doc-postgres-malformed-sha",
            user=limira.LimiraUser("user-a"),
            repo=repo,
            object_storage=storage,
        )

    assert exc.value.status_code == 404
    refreshed = repo.get_user_document("doc-postgres-malformed-sha", "user-a")
    assert refreshed is not None
    assert refreshed.public_dict()["download_url"] is None
    assert refreshed.metadata == {
        "source": "legacy",
        "download_unavailable": "document_sha_malformed",
    }
    persisted = json.loads(
        engine.uploaded_documents["doc-postgres-malformed-sha"]["metadata"]
    )
    assert persisted == {
        "source": "legacy",
        "download_unavailable": "document_sha_malformed",
    }


def test_postgres_repository_searches_uploaded_documents_by_vector():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-doc",
        owner_user_id="user-a",
        query="document query",
        scenario=None,
        runner_task_id="runner-doc",
    )
    for document_id, owner, task_id, filename, text, embedding in (
        (
            "doc-far",
            "user-a",
            "task-doc",
            "lexical-lithium.txt",
            "Lithium lithium.",
            [0.0, 1.0],
        ),
        ("doc-near", "user-a", "task-doc", "nickel.txt", "Nickel memo.", [1.0, 0.0]),
        ("doc-other-task", "user-a", None, "other.txt", "Nickel other.", [1.0, 0.0]),
        (
            "doc-foreign",
            "user-b",
            "task-doc",
            "foreign.txt",
            "Nickel foreign.",
            [1.0, 0.0],
        ),
    ):
        repo.record_uploaded_document(
            document_id=document_id,
            owner_user_id=owner,
            task_id=task_id,
            original_filename=filename,
            content_type="text/plain",
            byte_size=len(text),
            minio_bucket="limira-artifacts",
            object_key=f"limira/users/hash/uploads/{document_id}.txt",
            extracted_text=text,
            language=None,
            metadata={},
            embedding=embedding,
        )

    results = repo.search_user_documents(
        owner_user_id="user-a",
        task_id="task-doc",
        query="lithium",
        limit=2,
        query_embedding=[1.0, 0.0],
    )

    assert [result.document.document_id for result in results] == [
        "doc-near",
        "doc-far",
    ]
    assert results[0].score > results[1].score
    assert engine.vector_search_calls == [
        {
            "owner_user_id": "user-a",
            "task_id": "task-doc",
            "query_embedding": "[1,0]",
            "limit": 2,
        }
    ]


def test_postgres_repository_records_generated_report_pdf_metadata():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-report",
        owner_user_id="user-a",
        query="report query",
        scenario=None,
        runner_task_id="runner-report",
    )

    report = repo.record_generated_report(
        report_id="report-001",
        task_id="task-report",
        report_type="final",
        markdown="Final report [EVID-001]",
        html="<p>Final report [EVID-001]</p>",
        pdf_object_key="limira/users/hash/tasks/task-report/reports/report-001.pdf",
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={"pdf_sha256": "abc123", "pdf_size_bytes": 123},
    )

    assert report.report_id == "report-001"
    assert report.task_id == "task-report"
    assert report.pdf_object_key.endswith("/reports/report-001.pdf")
    assert report.evidence_refs == ["EVID-001"]
    assert report.metadata == {"pdf_sha256": "abc123", "pdf_size_bytes": 123}
    assert engine.generated_reports[("task-report", "report-001")]["pdf_object_key"].endswith(
        "/reports/report-001.pdf"
    )

    owned = repo.get_user_report(
        task_id="task-report",
        report_id="report-001",
        owner_user_id="user-a",
    )
    assert owned is not None
    assert owned.markdown == "Final report [EVID-001]"
    assert repo.get_user_report(
        task_id="task-report",
        report_id="report-001",
        owner_user_id="user-b",
    ) is None
    reports = repo.list_task_reports(task_id="task-report")
    assert [item.report_id for item in reports] == ["report-001"]
    assert reports[0].markdown == "Final report [EVID-001]"


@pytest.mark.asyncio
async def test_postgres_report_pdf_download_requires_same_task_report_binding():
    app, _memory_repo, storage = _limira_asgi_app()
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )

    async def task_repository_override():
        return repo

    app.dependency_overrides[limira.get_task_repository] = task_repository_override
    repo.create_task(
        task_id="task-owned-a",
        owner_user_id="user-a",
        query="same owner task a",
        scenario=None,
        runner_task_id="runner-task-owned-a",
    )
    repo.create_task(
        task_id="task-owned-b",
        owner_user_id="user-a",
        query="same owner task b",
        scenario=None,
        runner_task_id="runner-task-owned-b",
    )
    pdf_bytes = b"%PDF-1.7\nsame owner report\n%%EOF"
    pdf_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="reports",
        task_id="task-owned-b",
        filename="report-shared.pdf",
        object_id="report-shared",
    )
    await storage.put_object(
        object_key=pdf_key,
        data=pdf_bytes,
        content_type="application/pdf",
        metadata={"task_id": "task-owned-b", "report_id": "report-shared"},
    )
    repo.record_generated_report(
        report_id="report-shared",
        task_id="task-owned-b",
        report_type="final",
        markdown="Same owner report",
        html="<p>Same owner report</p>",
        pdf_object_key=pdf_key,
        evidence_refs=[],
        creator_user_id="user-a",
        metadata={
            "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "pdf_size_bytes": len(pdf_bytes),
        },
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limira.test",
    ) as client:
        wrong_task_response = await client.get(
            "/api/limira/tasks/task-owned-a/reports/report-shared/pdf"
        )
        correct_task_response = await client.get(
            "/api/limira/tasks/task-owned-b/reports/report-shared/pdf"
        )

    assert wrong_task_response.status_code == 404
    assert wrong_task_response.json()["detail"] == "report_not_found"
    assert wrong_task_response.content != pdf_bytes
    _assert_no_browser_leak(wrong_task_response.json())
    assert correct_task_response.status_code == 200
    assert correct_task_response.content == pdf_bytes
    assert correct_task_response.headers["content-type"].startswith("application/pdf")
    assert correct_task_response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_postgres_pdf_download_clears_invalid_persisted_pdf_object_key():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    repo.create_task(
        task_id="task-postgres-invalid-pdf-key",
        owner_user_id="user-a",
        query="postgres invalid pdf key",
        scenario=None,
        runner_task_id="runner-postgres-invalid-pdf-key",
    )
    repo.record_generated_report(
        report_id="report-postgres-invalid-key",
        task_id="task-postgres-invalid-pdf-key",
        report_type="final",
        markdown="Postgres report",
        html="<p>Postgres report</p>",
        pdf_object_key="../bad.pdf",
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={
            "pdf_bucket": "limira",
            "pdf_sha256": "bad-sha",
            "pdf_size_bytes": 456,
            "source": "postgres-cache",
        },
    )

    with pytest.raises(HTTPException) as exc:
        await limira.download_task_report_pdf(
            "task-postgres-invalid-pdf-key",
            "report-postgres-invalid-key",
            user=limira.LimiraUser("user-a"),
            repo=repo,
            object_storage=storage,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "report_pdf_not_found"
    refreshed = repo.get_user_report(
        task_id="task-postgres-invalid-pdf-key",
        report_id="report-postgres-invalid-key",
        owner_user_id="user-a",
    )
    assert refreshed is not None
    assert refreshed.pdf_object_key is None
    public_refreshed = refreshed.public_dict()
    assert public_refreshed["pdf_url"] is None
    assert public_refreshed["pdf_sha256"] is None
    assert public_refreshed["pdf_size_bytes"] is None
    assert refreshed.metadata == {"source": "postgres-cache"}
    persisted = engine.generated_reports[
        ("task-postgres-invalid-pdf-key", "report-postgres-invalid-key")
    ]
    assert persisted["pdf_object_key"] is None


@pytest.mark.asyncio
async def test_postgres_pdf_download_clears_missing_persisted_pdf_object():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    repo.create_task(
        task_id="task-postgres-missing-pdf",
        owner_user_id="user-a",
        query="postgres missing pdf",
        scenario=None,
        runner_task_id="runner-postgres-missing-pdf",
    )
    pdf_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="reports",
        task_id="task-postgres-missing-pdf",
        filename="report-postgres-missing.pdf",
        object_id="report-postgres-missing",
    )
    repo.record_generated_report(
        report_id="report-postgres-missing",
        task_id="task-postgres-missing-pdf",
        report_type="final",
        markdown="Postgres report",
        html="<p>Postgres report</p>",
        pdf_object_key=pdf_key,
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={
            "pdf_bucket": "limira",
            "pdf_sha256": hashlib.sha256(b"expected pdf bytes").hexdigest(),
            "pdf_size_bytes": 18,
            "source": "postgres-cache",
        },
    )

    with pytest.raises(HTTPException) as exc:
        await limira.download_task_report_pdf(
            "task-postgres-missing-pdf",
            "report-postgres-missing",
            user=limira.LimiraUser("user-a"),
            repo=repo,
            object_storage=storage,
        )

    assert exc.value.status_code == 404
    refreshed = repo.get_user_report(
        task_id="task-postgres-missing-pdf",
        report_id="report-postgres-missing",
        owner_user_id="user-a",
    )
    assert refreshed is not None
    assert refreshed.pdf_object_key is None
    assert refreshed.public_dict()["pdf_url"] is None
    assert refreshed.metadata == {"source": "postgres-cache"}
    persisted = engine.generated_reports[
        ("task-postgres-missing-pdf", "report-postgres-missing")
    ]
    assert persisted["pdf_object_key"] is None


@pytest.mark.asyncio
async def test_postgres_pdf_download_clears_mismatched_persisted_pdf_object():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    repo.create_task(
        task_id="task-postgres-mismatch-pdf",
        owner_user_id="user-a",
        query="postgres mismatch pdf",
        scenario=None,
        runner_task_id="runner-postgres-mismatch-pdf",
    )
    expected_sha = hashlib.sha256(b"expected pdf bytes").hexdigest()
    mismatched_bytes = b"not the expected pdf bytes"
    pdf_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="reports",
        task_id="task-postgres-mismatch-pdf",
        filename="report-postgres-mismatch.pdf",
        object_id="report-postgres-mismatch",
    )
    await storage.put_object(
        object_key=pdf_key,
        data=mismatched_bytes,
        content_type="application/pdf",
        metadata={"report_id": "report-postgres-mismatch"},
    )
    repo.record_generated_report(
        report_id="report-postgres-mismatch",
        task_id="task-postgres-mismatch-pdf",
        report_type="final",
        markdown="Postgres report",
        html="<p>Postgres report</p>",
        pdf_object_key=pdf_key,
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={
            "pdf_bucket": "limira",
            "pdf_sha256": expected_sha,
            "pdf_size_bytes": len(b"expected pdf bytes"),
            "source": "postgres-cache",
        },
    )

    with pytest.raises(HTTPException) as exc:
        await limira.download_task_report_pdf(
            "task-postgres-mismatch-pdf",
            "report-postgres-mismatch",
            user=limira.LimiraUser("user-a"),
            repo=repo,
            object_storage=storage,
        )

    assert exc.value.status_code == 404
    refreshed = repo.get_user_report(
        task_id="task-postgres-mismatch-pdf",
        report_id="report-postgres-mismatch",
        owner_user_id="user-a",
    )
    assert refreshed is not None
    assert refreshed.pdf_object_key is None
    assert refreshed.public_dict()["pdf_url"] is None
    assert refreshed.metadata == {"source": "postgres-cache"}
    persisted = engine.generated_reports[
        ("task-postgres-mismatch-pdf", "report-postgres-mismatch")
    ]
    assert persisted["pdf_object_key"] is None


@pytest.mark.asyncio
async def test_postgres_pdf_download_clears_blank_persisted_pdf_object():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    repo.create_task(
        task_id="task-postgres-blank-pdf",
        owner_user_id="user-a",
        query="postgres blank pdf",
        scenario=None,
        runner_task_id="runner-postgres-blank-pdf",
    )
    blank_pdf_bytes = (
        b"%PDF-1.4\n"
        b"3 0 obj\n"
        b"<</Length 0>> stream\n"
        b"endstream\n"
        b"endobj\n"
        b"%%EOF"
    )
    pdf_key = limira.build_limira_object_key(
        owner_user_id="user-a",
        category="reports",
        task_id="task-postgres-blank-pdf",
        filename="report-postgres-blank.pdf",
        object_id="report-postgres-blank",
    )
    await storage.put_object(
        object_key=pdf_key,
        data=blank_pdf_bytes,
        content_type="application/pdf",
        metadata={"report_id": "report-postgres-blank"},
    )
    repo.record_generated_report(
        report_id="report-postgres-blank",
        task_id="task-postgres-blank-pdf",
        report_type="final",
        markdown="Postgres report",
        html="<p>Postgres report</p>",
        pdf_object_key=pdf_key,
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={
            "pdf_bucket": "limira",
            "pdf_sha256": hashlib.sha256(blank_pdf_bytes).hexdigest(),
            "pdf_size_bytes": len(blank_pdf_bytes),
            "source": "postgres-cache",
        },
    )

    with pytest.raises(HTTPException) as exc:
        await limira.download_task_report_pdf(
            "task-postgres-blank-pdf",
            "report-postgres-blank",
            user=limira.LimiraUser("user-a"),
            repo=repo,
            object_storage=storage,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "report_pdf_not_found"
    refreshed = repo.get_user_report(
        task_id="task-postgres-blank-pdf",
        report_id="report-postgres-blank",
        owner_user_id="user-a",
    )
    assert refreshed is not None
    assert refreshed.pdf_object_key is None
    assert refreshed.public_dict()["pdf_url"] is None
    assert refreshed.metadata == {"source": "postgres-cache"}
    persisted = engine.generated_reports[
        ("task-postgres-blank-pdf", "report-postgres-blank")
    ]
    assert persisted["pdf_object_key"] is None


def test_postgres_repository_invalidates_archive_metadata_on_task_scoped_writes():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-archive-invalidated",
        owner_user_id="user-a",
        query="archive invalidation",
        scenario=None,
        runner_task_id="runner-archive-invalidated",
    )

    repo.update_task(
        "task-archive-invalidated",
        archive_status="ready",
        archive_object_key="limira/users/hash/tasks/task-archive-invalidated/archives/a.zip",
        archive_zip_sha256="sha-a",
    )
    repo.record_generated_report(
        report_id="report-invalidates",
        task_id="task-archive-invalidated",
        report_type="final",
        markdown="new report",
        html=None,
        pdf_object_key=None,
        evidence_refs=[],
        creator_user_id="user-a",
        metadata={},
    )
    assert engine.tasks["task-archive-invalidated"]["archive_object_key"] is None
    assert engine.tasks["task-archive-invalidated"]["archive_zip_sha256"] is None

    repo.update_task(
        "task-archive-invalidated",
        archive_object_key="limira/users/hash/tasks/task-archive-invalidated/archives/b.zip",
        archive_zip_sha256="sha-b",
    )
    repo.record_uploaded_document(
        document_id="doc-invalidates",
        owner_user_id="user-a",
        task_id="task-archive-invalidated",
        original_filename="brief.txt",
        content_type="text/plain",
        byte_size=5,
        minio_bucket="limira-artifacts",
        object_key="limira/users/hash/tasks/task-archive-invalidated/uploads/doc.txt",
        extracted_text="brief",
        language=None,
        metadata={},
    )
    assert engine.tasks["task-archive-invalidated"]["archive_object_key"] is None
    assert engine.tasks["task-archive-invalidated"]["archive_zip_sha256"] is None

    repo.update_task(
        "task-archive-invalidated",
        archive_object_key="limira/users/hash/tasks/task-archive-invalidated/archives/c.zip",
        archive_zip_sha256="sha-c",
    )
    repo.record_artifact(
        "task-archive-invalidated",
        "evidence",
        {
            "evidence_id": "EVID-INVALIDATES",
            "summary": "new evidence",
            "source_url": "https://example.test/source",
        },
    )
    assert engine.tasks["task-archive-invalidated"]["archive_object_key"] is None
    assert engine.tasks["task-archive-invalidated"]["archive_zip_sha256"] is None

    repo.update_task(
        "task-archive-invalidated",
        archive_object_key="limira/users/hash/tasks/task-archive-invalidated/archives/d.zip",
        archive_zip_sha256="sha-d",
    )
    repo.record_uploaded_document(
        document_id="doc-unlinked-does-not-invalidate",
        owner_user_id="user-a",
        task_id=None,
        original_filename="unlinked.txt",
        content_type="text/plain",
        byte_size=8,
        minio_bucket="limira-artifacts",
        object_key="limira/users/hash/uploads/doc-unlinked.txt",
        extracted_text="unlinked",
        language=None,
        metadata={},
    )
    assert (
        engine.tasks["task-archive-invalidated"]["archive_object_key"]
        == "limira/users/hash/tasks/task-archive-invalidated/archives/d.zip"
    )
    assert engine.tasks["task-archive-invalidated"]["archive_zip_sha256"] == "sha-d"


@pytest.mark.asyncio
async def test_event_proxy_streams_runner_events_and_populates_artifacts():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    raw_secret_values = [
        "OPENAI_API_KEY=sk-runnersecret123456",
        "Authorization: Bearer runner-secret-123456",
        "RUNNER_SERVICE_TOKEN=runner-service-secret-123456",
        "OPENAI_API_KEY=sk-artifactkeysecret123456",
        "Authorization: Bearer artifact-key-secret-123456",
    ]
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "status",
                "payload": {"status": "running"},
            },
            {
                "task_id": "runner-task-a",
                "type": "evidence_collected",
                "payload": {
                    "title": "Export control notice",
                    "summary": f"Policy update {raw_secret_values[0]}",
                    "source_url": (
                        "https://example.test/source?token=runner-query-secret-123456"
                    ),
                    raw_secret_values[3]: "secret-bearing evidence key",
                    "nested": {
                        raw_secret_values[4]: "secret-bearing nested key",
                    },
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "entity_extracted",
                "payload": {
                    "entity_id": "ENT-001",
                    "name": "United States",
                    "entity_type": "country",
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "record_research_artifact",
                "payload": {
                    "artifact_type": "report_section",
                    "payload": {
                        "title": "Assessment",
                        raw_secret_values[4]: "secret-bearing report key",
                        "markdown": (
                            "Finding references [EVID-001]\n\n"
                            f"{raw_secret_values[1]}\n{raw_secret_values[2]}"
                        ),
                    },
                    "evidence_refs": ["EVID-001"],
                    "confidence": 0.8,
                    "notes": raw_secret_values[2],
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "relation_extracted",
                "payload": "not-a-dict",
            },
        ]
    )

    created = await limira.create_research_task(
        {"query": "semiconductor export controls"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert [event["type"] for event in events] == [
        "status",
        "evidence_collected",
        "entity_extracted",
        "record_research_artifact",
        "relation_extracted",
        "artifact_warning",
        "status",
    ]
    assert all(event["task_id"] == task_id for event in events)
    assert events[-1]["payload"]["status"] == "completed"
    assert events[-1]["payload"]["status_source"] == "runner"
    _assert_no_browser_leak(events)
    serialized_events = json.dumps(events, ensure_ascii=False)
    for secret in raw_secret_values:
        assert secret not in serialized_events
    assert "runner-query-secret-123456" not in serialized_events
    assert limira.LIMIRA_SECRET_REDACTION in serialized_events

    task = repo.get_task(task_id)
    assert task.status == "completed"
    assert task.archive_status == "ready"
    assert research.stream_calls[0]["task"].runner_task_id == "runner-task-a"

    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    assert artifacts["evidence"][0]["evidence_id"] == "EVID-001"
    assert artifacts["evidence"][0]["title"] == "Export control notice"
    assert limira.LIMIRA_SECRET_REDACTION in artifacts["evidence"][0]["summary"]
    assert "%5BREDACTED%5D" in artifacts["evidence"][0]["source_url"]
    assert "runner-query-secret-123456" not in artifacts["evidence"][0]["source_url"]
    assert artifacts["entities"][0]["entity_id"] == "ENT-001"
    assert artifacts["report_sections"][0]["evidence_refs"] == ["EVID-001"]
    assert artifacts["report_sections"][0]["confidence"] == 0.8
    assert artifacts["relations"] == []
    _assert_no_browser_leak(artifacts)
    serialized_artifacts = json.dumps(artifacts, ensure_ascii=False)
    for secret in raw_secret_values:
        assert secret not in serialized_artifacts
    assert "runner-query-secret-123456" not in serialized_artifacts

    archive_response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    members = _archive_member_texts(archive_response.body)
    serialized_members = json.dumps(members, ensure_ascii=False)
    for secret in raw_secret_values:
        assert secret not in serialized_members
    assert "runner-query-secret-123456" not in serialized_members
    assert limira.LIMIRA_SECRET_REDACTION in members["report.md"]
    assert limira.LIMIRA_SECRET_REDACTION in members["trace.json"]
    trace = json.loads(members["trace.json"])
    assert [event["type"] for event in trace["artifact_events"]] == [
        "evidence_collected",
        "entity_extracted",
        "report_section_generated",
        "artifact_warning",
    ]
    assert trace["artifact_events"][2]["source_event_type"] == (
        "record_research_artifact"
    )
    assert trace["artifact_events"][2]["local_artifact_id"] == "REPORT-001"
    assert trace["artifact_warnings"][0]["payload"]["warning"] == (
        "invalid_artifact_payload"
    )
    assert trace["artifact_warnings"][0]["payload"]["source_event_type"] == (
        "relation_extracted"
    )


@pytest.mark.asyncio
async def test_event_proxy_scrubs_runner_status_and_error_payloads_before_browser_runtime_and_task_state():
    repo = limira.InMemoryLimiraTaskRepository()
    runtime_state = limira.InMemoryLimiraRuntimeState()
    user = limira.LimiraUser("user-a")
    raw_secret_values = [
        "OPENAI_API_KEY=sk-statussecret123456",
        "Authorization: Bearer status-secret-123456",
        "RUNNER_SERVICE_TOKEN=status-runner-token-123456",
        "OPENAI_API_KEY=sk-timestampsecret123456",
        "Authorization: Bearer timestamp-secret-123456",
        "OPENAI_API_KEY=sk-statuskeysecret123456",
        "Authorization: Bearer error-key-secret-123456",
    ]
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "status",
                "timestamp": f"{raw_secret_values[3]} {raw_secret_values[4]}",
                "payload": {
                    "status": "running",
                    "error": f"{raw_secret_values[0]} {raw_secret_values[1]}",
                    raw_secret_values[5]: "secret-bearing status key",
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "error",
                "timestamp": f"{raw_secret_values[4]} {raw_secret_values[3]}",
                "payload": {
                    "error": f"runner failed {raw_secret_values[2]}",
                    raw_secret_values[6]: "secret-bearing error key",
                },
            },
        ]
    )

    created = await limira.create_research_task(
        {"query": "status secret scrub"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    runtime_snapshot = await runtime_state.get_task_runtime(task_id)
    task_payload = await limira.get_task(task_id, user=user, repo=repo)

    assert [event["type"] for event in events] == ["status", "error"]
    assert all(event["task_id"] == task_id for event in events)
    assert limira.LIMIRA_SECRET_REDACTION in events[0]["timestamp"]
    assert limira.LIMIRA_SECRET_REDACTION in events[1]["timestamp"]
    assert limira.LIMIRA_SECRET_REDACTION in runtime_snapshot["last_event"]["timestamp"]
    serialized = json.dumps(
        {
            "events": events,
            "runtime": runtime_snapshot,
            "task": task_payload,
            "repo_error": repo.get_task(task_id).error,
        },
        ensure_ascii=False,
    )
    for secret in raw_secret_values:
        assert secret not in serialized
    assert limira.LIMIRA_SECRET_REDACTION in serialized
    assert task_payload["status"] == "failed"
    assert task_payload["error"].startswith("runner failed")
    assert runtime_snapshot["error"].startswith("runner failed")
    _assert_no_browser_leak(events)
    _assert_no_browser_leak(task_payload)


@pytest.mark.asyncio
async def test_event_proxy_shapes_runner_status_and_error_internal_details_before_browser_runtime_and_task_state():
    repo = limira.InMemoryLimiraTaskRepository()
    runtime_state = limira.InMemoryLimiraRuntimeState()
    user = limira.LimiraUser("user-a")
    internal_error = (
        "runner status failed at http://10.20.30.40:8091/internal/tasks/runner-task-a "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt "
        "OPENAI_API_KEY=sk-streamstatusinternal123456"
    )
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "status",
                "payload": {
                    "status": "running",
                    "archive_status": "pending",
                    "error": internal_error,
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "error",
                "payload": {"error": internal_error},
            },
        ]
    )

    created = await limira.create_research_task(
        {"query": "status internal detail shaping"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    runtime_snapshot = await runtime_state.get_task_runtime(task_id)
    task_payload = await limira.get_task(task_id, user=user, repo=repo)

    assert [event["type"] for event in events] == ["status", "error"]
    assert events[0]["payload"]["error"] == "runner_task_failed"
    assert events[1]["payload"]["error"] == "runner_task_failed"
    assert repo.get_task(task_id).error == "runner_task_failed"
    assert runtime_snapshot["error"] == "runner_task_failed"
    assert runtime_snapshot["last_event"]["payload"]["error"] == "runner_task_failed"
    assert task_payload["error"] == "runner_task_failed"
    serialized = json.dumps(
        {
            "events": events,
            "runtime": runtime_snapshot,
            "task": task_payload,
            "repo_error": repo.get_task(task_id).error,
        },
        ensure_ascii=False,
    )
    for leaked in (
        "http://10.20.30.40:8091",
        "limira/users/hash",
        "sk-streamstatusinternal123456",
        "OPENAI_API_KEY",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(events)
    _assert_no_browser_leak(task_payload)


@pytest.mark.asyncio
async def test_event_proxy_shapes_non_status_runner_event_internal_error_fields_before_browser_runtime_state():
    repo = limira.InMemoryLimiraTaskRepository()
    runtime_state = limira.InMemoryLimiraRuntimeState()
    user = limira.LimiraUser("user-a")
    internal_detail = (
        "progress failed at http://10.20.30.40:8091/internal/tasks/runner-task-a "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt "
        "OPENAI_API_KEY=sk-progressinternal123456"
    )
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "progress",
                "payload": {
                    "status": "failed",
                    "archive_status": "failed",
                    "error": internal_detail,
                    "warning": internal_detail,
                    "reason": internal_detail,
                    "data": {
                        "error": internal_detail,
                        "warning": internal_detail,
                        "reason": internal_detail,
                    },
                },
            }
        ]
    )

    created = await limira.create_research_task(
        {"query": "progress internal detail shaping"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    runtime_snapshot = await runtime_state.get_task_runtime(task_id)
    task_payload = await limira.get_task(task_id, user=user, repo=repo)

    assert [event["type"] for event in events] == ["progress"]
    payload = events[0]["payload"]
    assert payload["error"] == "runner_task_failed"
    assert payload["warning"] == "runner_status_warning"
    assert payload["reason"] == "runner_stream_conflict"
    assert payload["data"]["error"] == "runner_task_failed"
    assert payload["data"]["warning"] == "runner_status_warning"
    assert payload["data"]["reason"] == "runner_stream_conflict"
    assert repo.get_task(task_id).error == "runner_task_failed"
    assert runtime_snapshot["error"] == "runner_task_failed"
    assert runtime_snapshot["last_warning"] == "runner_status_warning"
    assert runtime_snapshot["last_event"]["payload"]["error"] == "runner_task_failed"
    assert runtime_snapshot["last_event"]["payload"]["data"]["error"] == (
        "runner_task_failed"
    )
    assert task_payload["error"] == "runner_task_failed"
    serialized = json.dumps(
        {
            "events": events,
            "runtime": runtime_snapshot,
            "task": task_payload,
            "repo_error": repo.get_task(task_id).error,
        },
        ensure_ascii=False,
    )
    for leaked in (
        "http://10.20.30.40:8091",
        "limira/users/hash",
        "sk-progressinternal123456",
        "OPENAI_API_KEY",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(events)
    _assert_no_browser_leak(task_payload)


@pytest.mark.asyncio
async def test_authoritative_runner_status_scrubs_persisted_terminal_task_error_and_archive_metadata():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    runtime_state = limira.InMemoryLimiraRuntimeState()
    user = limira.LimiraUser("user-a")
    raw_secret_values = [
        "OPENAI_API_KEY=sk-authoritativestatus123456",
        "Authorization: Bearer authoritative-secret-123456",
        "RUNNER_SERVICE_TOKEN=authoritative-runner-token-123456",
    ]
    research = FakeResearchClient(
        events=[],
        status_payload={
            "task_id": "runner-task-a",
            "status": "failed",
            "archive_status": "failed",
            "error": " ".join(raw_secret_values),
        },
    )

    created = await limira.create_research_task(
        {"query": "authoritative secret scrub"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    first_response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    first_events = _parse_sse_chunks(
        [chunk async for chunk in first_response.body_iterator]
    )
    task = repo.get_task(task_id)
    assert task.status == "failed"
    assert task.error
    assert limira.LIMIRA_SECRET_REDACTION in task.error

    terminal_response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    terminal_events = _parse_sse_chunks(
        [chunk async for chunk in terminal_response.body_iterator]
    )
    task_payload = await limira.get_task(task_id, user=user, repo=repo)

    repo.update_task(task_id, archive_status="ready")
    archive_response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    archive_members = _archive_member_texts(archive_response.body)

    serialized = json.dumps(
        {
            "first_events": first_events,
            "terminal_events": terminal_events,
            "task": task_payload,
            "archive": archive_members,
            "repo_error": task.error,
        },
        ensure_ascii=False,
    )
    for secret in raw_secret_values:
        assert secret not in serialized
    assert limira.LIMIRA_SECRET_REDACTION in serialized
    assert terminal_events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "failed",
                "archive_status": "failed",
                "terminal": True,
                "error": task.error,
            },
        }
    ]
    assert task_payload["error"] == task.error
    assert limira.LIMIRA_SECRET_REDACTION in archive_members["metadata.json"]
    assert limira.LIMIRA_SECRET_REDACTION in archive_members["trace.json"]
    _assert_no_browser_leak(first_events)
    _assert_no_browser_leak(terminal_events)
    _assert_no_browser_leak(task_payload)


@pytest.mark.asyncio
async def test_authoritative_runner_status_hides_internal_error_details_from_browser_state():
    repo = limira.InMemoryLimiraTaskRepository()
    runtime_state = limira.InMemoryLimiraRuntimeState()
    user = limira.LimiraUser("user-a")
    internal_error = (
        "runner failed GET http://10.20.30.40:8091/internal/tasks/runner-task-a "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt "
        "OPENAI_API_KEY=sk-authoritativeinternal123456"
    )
    research = FakeResearchClient(
        events=[],
        status_payload={
            "task_id": "runner-task-a",
            "status": "failed",
            "archive_status": "failed",
            "error": internal_error,
        },
    )

    created = await limira.create_research_task(
        {"query": "authoritative internal error"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    first_response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    first_events = _parse_sse_chunks(
        [chunk async for chunk in first_response.body_iterator]
    )
    task = repo.get_task(task_id)
    task_payload = await limira.get_task(task_id, user=user, repo=repo)

    terminal_response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    terminal_events = _parse_sse_chunks(
        [chunk async for chunk in terminal_response.body_iterator]
    )

    assert task is not None
    assert task.error == "runner_task_failed"
    assert task_payload["error"] == "runner_task_failed"
    assert terminal_events[-1]["payload"]["error"] == "runner_task_failed"
    serialized = json.dumps(
        {
            "events": first_events,
            "terminal_events": terminal_events,
            "task": task_payload,
            "repo_error": task.error,
        },
        ensure_ascii=False,
    )
    for leaked in (
        "http://10.20.30.40:8091",
        "limira/users/hash",
        "sk-authoritativeinternal123456",
        "OPENAI_API_KEY",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(first_events)
    _assert_no_browser_leak(terminal_events)
    _assert_no_browser_leak(task_payload)


@pytest.mark.asyncio
async def test_event_proxy_persists_final_summary_show_text_as_report_section():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    raw_secret_values = [
        "OPENAI_API_KEY=sk-finalopenai123456",
        "Bearer final-bearer-token-123456",
        "RUNNER_SERVICE_TOKEN=final-runner-token-123456",
    ]
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "start_of_agent",
                "payload": {"agent_name": "Final Summary", "agent_id": "agent-final"},
            },
            {
                "task_id": "runner-task-a",
                "type": "tool_call",
                "payload": {
                    "tool_name": "show_text",
                    "tool_input": {
                        "text": (
                            "# Final answer\n\n"
                            "BYD is not on the active list. [EVID-001]\n\n"
                            + "\n".join(raw_secret_values)
                        )
                    },
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "end_of_agent",
                "payload": {"agent_name": "Final Summary", "agent_id": "agent-final"},
            },
        ]
    )

    created = await limira.create_research_task(
        {"query": "BYD 1260H"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert [event["type"] for event in events] == [
        "start_of_agent",
        "tool_call",
        "report_section_generated",
        "end_of_agent",
        "status",
    ]
    serialized_events = json.dumps(events, ensure_ascii=False)
    for secret in raw_secret_values:
        assert secret not in serialized_events
    assert limira.LIMIRA_SECRET_REDACTION in serialized_events
    assert limira.LIMIRA_SECRET_REDACTION in json.dumps(
        events[1]["payload"]["tool_input"],
        ensure_ascii=False,
    )
    report_event = events[2]
    assert report_event["payload"]["title"] == "最终回答"
    assert report_event["payload"]["evidence_refs"] == ["EVID-001"]
    for secret in raw_secret_values:
        assert secret not in json.dumps(report_event, ensure_ascii=False)

    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    assert len(artifacts["report_sections"]) == 1
    assert artifacts["report_sections"][0]["markdown"].startswith("# Final answer")
    assert artifacts["report_sections"][0]["source_event_type"] == "final_summary_show_text"
    assert limira.LIMIRA_SECRET_REDACTION in artifacts["report_sections"][0]["markdown"]
    for secret in raw_secret_values:
        assert secret not in json.dumps(artifacts, ensure_ascii=False)

    archive_response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    members = _archive_member_texts(archive_response.body)
    assert "# Final answer" in members["report.md"]
    assert limira.LIMIRA_SECRET_REDACTION in members["report.md"]
    assert limira.LIMIRA_SECRET_REDACTION in members["trace.json"]
    for secret in raw_secret_values:
        assert secret not in json.dumps(members, ensure_ascii=False)
    trace = json.loads(members["trace.json"])
    assert trace["artifact_events"][0]["type"] == "report_section_generated"
    assert trace["artifact_events"][0]["source_event_type"] == "final_summary_show_text"


@pytest.mark.asyncio
async def test_event_proxy_unwraps_json_wrapped_final_summary_report_text():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    report_body = (
        "## 研究结论\n\n"
        "**比亚迪(BYD Company Limited) 目前已在已生效的1260H清单上。** "
        "[EVID-001]"
    )
    wrapped_report = json.dumps(
        {
            "id": "REPORT-001",
            "title": "比亚迪(BYD) Section 1260H 清单状态研究报告",
            "content": report_body,
        },
        ensure_ascii=False,
    )
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "start_of_agent",
                "payload": {"agent_name": "Final Summary", "agent_id": "agent-final"},
            },
            {
                "task_id": "runner-task-a",
                "type": "tool_call",
                "payload": {
                    "tool_name": "show_text",
                    "tool_input": {"text": wrapped_report},
                },
            },
        ]
    )

    created = await limira.create_research_task(
        {"query": "BYD 1260H"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    serialized_events = json.dumps(events, ensure_ascii=False)
    assert '"content"' not in serialized_events
    assert wrapped_report not in serialized_events
    assert events[1]["payload"]["tool_input"]["text"] == report_body
    assert events[2]["payload"]["title"] == (
        "比亚迪(BYD) Section 1260H 清单状态研究报告"
    )
    assert events[2]["payload"]["markdown"] == report_body
    assert events[2]["payload"]["evidence_refs"] == ["EVID-001"]

    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    assert artifacts["report_sections"][0]["title"] == (
        "比亚迪(BYD) Section 1260H 清单状态研究报告"
    )
    assert artifacts["report_sections"][0]["markdown"] == report_body
    assert '"content"' not in json.dumps(artifacts, ensure_ascii=False)

    archive_response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    members = _archive_member_texts(archive_response.body)
    assert report_body in members["report.md"]
    assert wrapped_report not in json.dumps(members, ensure_ascii=False)
    assert '"content"' not in members["trace.json"]


@pytest.mark.asyncio
async def test_artifacts_api_unwraps_legacy_json_wrapped_report_section_text():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-legacy-json-report",
        owner_user_id=user.id,
        query="BYD 1260H",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    report_body = "## 研究结论\n\n旧数据应只显示正文。 [EVID-001]"
    wrapped_report = json.dumps(
        {
            "id": "REPORT-099",
            "title": "旧报告标题",
            "content": report_body,
        },
        ensure_ascii=False,
    )
    repo.record_artifact(
        task.task_id,
        "report_section",
        {
            "section_id": "REPORT-099",
            "markdown": wrapped_report,
            "source_event_type": "record_research_artifact",
        },
    )
    repo.update_task(task.task_id, status="completed", archive_status="ready")

    artifacts = await limira.get_task_artifacts(task.task_id, user=user, repo=repo)

    section = artifacts["report_sections"][0]
    assert section["title"] == "旧报告标题"
    assert section["markdown"] == report_body
    serialized_artifacts = json.dumps(artifacts, ensure_ascii=False)
    assert wrapped_report not in serialized_artifacts
    assert '"content"' not in serialized_artifacts

    archive_response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    members = _archive_member_texts(archive_response.body)
    assert report_body in members["report.md"]
    assert wrapped_report not in json.dumps(members, ensure_ascii=False)
    assert '"content"' not in members["trace.json"]


@pytest.mark.asyncio
async def test_archive_download_unwraps_json_wrapped_report_members_from_persisted_archive():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    task = repo.create_task(
        task_id="task-legacy-json-archive",
        owner_user_id=user.id,
        query="BYD 1260H",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    report_body = "## 研究结论\n\n历史 archive 应只下载正文。 [EVID-001]"
    wrapped_report = json.dumps(
        {
            "id": "REPORT-007",
            "title": "历史 archive 标题",
            "content": report_body,
        },
        ensure_ascii=False,
    )
    raw_archive = _archive_zip_with_json_wrapped_report(
        task_id=task.task_id,
        owner_user_id=user.id,
        report_text=wrapped_report,
    )
    archive_key = limira.build_limira_object_key(
        owner_user_id=user.id,
        category="archives",
        task_id=task.task_id,
        filename="archive.zip",
        object_id=task.task_id,
    )
    stored = await storage.put_object(
        object_key=archive_key,
        data=raw_archive,
        content_type="application/zip",
        metadata={
            "task_id": task.task_id,
            "owner_user_id": user.id,
            "archive_sha256": hashlib.sha256(raw_archive).hexdigest(),
        },
    )
    repo.update_task(
        task.task_id,
        status="completed",
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )

    response = await limira.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.body != raw_archive
    members = _archive_member_texts(response.body)
    assert members["report.md"] == report_body
    assert "历史 archive 应只下载正文。" in members["report.html"]
    assert "研究结论" in members["report.html"]
    assert wrapped_report not in json.dumps(members, ensure_ascii=False)
    assert '"content"' not in members["trace.json"]
    assert task.archive_object_key == stored.object_key
    assert task.archive_zip_sha256 == hashlib.sha256(response.body).hexdigest()
    repaired = storage.objects[stored.object_key]
    assert repaired["data"] == response.body
    assert repaired["sha256"] == task.archive_zip_sha256


@pytest.mark.asyncio
async def test_postgres_event_proxy_persists_final_summary_show_text_as_report_section():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    raw_secret_values = [
        "OPENAI_API_KEY=sk-finalpgopenai123456",
        "Bearer final-pg-bearer-token-123456",
        "RUNNER_SERVICE_TOKEN=final-pg-runner-token-123456",
    ]
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "start_of_agent",
                "payload": {"agent_name": "Final Summary", "agent_id": "agent-final"},
            },
            {
                "task_id": "runner-task-a",
                "type": "tool_call",
                "payload": {
                    "tool_name": "show_text",
                    "tool_input": {
                        "text": (
                            "# Final answer\n\n"
                            "BYD is not on the active list. [EVID-001]\n\n"
                            + "\n".join(raw_secret_values)
                        )
                    },
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "end_of_agent",
                "payload": {"agent_name": "Final Summary", "agent_id": "agent-final"},
            },
        ]
    )

    created = await limira.create_research_task(
        {"query": "BYD 1260H"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert [event["type"] for event in events] == [
        "start_of_agent",
        "tool_call",
        "report_section_generated",
        "end_of_agent",
        "status",
    ]
    report_event = events[2]
    assert report_event["payload"]["title"] == "最终回答"
    assert report_event["payload"]["evidence_refs"] == ["EVID-001"]
    serialized_events = json.dumps(events, ensure_ascii=False)
    for secret in raw_secret_values:
        assert secret not in serialized_events
    assert limira.LIMIRA_SECRET_REDACTION in serialized_events
    _assert_no_browser_leak(events)

    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    assert len(artifacts["report_sections"]) == 1
    report_section = artifacts["report_sections"][0]
    assert report_section["section_id"] == "REPORT-001"
    assert report_section["markdown"].startswith("# Final answer")
    assert report_section["source_event_type"] == "final_summary_show_text"
    assert limira.LIMIRA_SECRET_REDACTION in report_section["markdown"]
    for secret in raw_secret_values:
        assert secret not in json.dumps(artifacts, ensure_ascii=False)

    persisted_report = engine.generated_reports[(task_id, "REPORT-001")]
    assert persisted_report["creator_user_id"] == "user-a"
    assert persisted_report["markdown"] == report_section["markdown"]
    assert persisted_report["evidence_refs"] == ["EVID-001"]
    assert json.loads(persisted_report["metadata"])["source_event_type"] == (
        "final_summary_show_text"
    )

    reports = repo.list_task_reports(task_id=task_id)
    assert [report.report_id for report in reports] == ["REPORT-001"]
    assert reports[0].markdown == report_section["markdown"]

    archive_response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    members = _archive_member_texts(archive_response.body)
    assert "# Final answer" in members["report.md"]
    assert limira.LIMIRA_SECRET_REDACTION in members["report.md"]
    assert limira.LIMIRA_SECRET_REDACTION in members["trace.json"]
    for secret in raw_secret_values:
        assert secret not in json.dumps(members, ensure_ascii=False)
    trace = json.loads(members["trace.json"])
    assert trace["artifact_events"][0]["type"] == "report_section_generated"
    assert trace["artifact_events"][0]["source_event_type"] == (
        "final_summary_show_text"
    )
    assert trace["artifact_events"][0]["local_artifact_id"] == "REPORT-001"
    assert repo.get_task(task_id).archive_status == "ready"
    assert repo.get_task(task_id).archive_object_key in storage.objects
    _assert_no_browser_leak(members)


@pytest.mark.asyncio
async def test_record_research_artifact_tool_call_reaches_artifacts_and_archive_trace():
    from pipeline_helpers import filter_message

    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    artifact_event = filter_message(
        {
            "event": "tool_call",
            "data": {
                "tool_name": "record_research_artifact",
                "tool_input": {
                    "artifact_type": "evidence",
                    "payload": {
                        "title": "Port authority bulletin",
                        "source_url": "https://example.test/port",
                        "summary": "New inspection rule published",
                    },
                    "evidence_refs": ["EVID-001"],
                    "confidence": 0.9,
                    "notes": "MCP tool path",
                },
            },
        }
    )
    research = FakeResearchClient(events=[artifact_event])

    created = await limira.create_research_task(
        {"query": "port inspection OSINT"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events[0]["type"] == "evidence_collected"
    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    assert artifacts["evidence"][0]["evidence_id"] == "EVID-001"
    assert artifacts["evidence"][0]["title"] == "Port authority bulletin"
    assert artifacts["evidence"][0]["notes"] == "MCP tool path"
    assert repo.get_task(task_id).archive_status == "ready"

    archive_response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    trace = json.loads(_archive_member_texts(archive_response.body)["trace.json"])

    assert trace["artifacts"]["evidence"][0]["evidence_id"] == "EVID-001"
    assert trace["artifacts"]["evidence"][0]["title"] == "Port authority bulletin"
    assert trace["artifacts"]["evidence"][0]["source_event_type"] == (
        "record_research_artifact"
    )
    assert trace["artifact_events"][0]["type"] == "evidence_collected"
    assert trace["artifact_events"][0]["artifact_type"] == "evidence"
    assert trace["artifact_events"][0]["bucket"] == "evidence"
    assert trace["artifact_events"][0]["local_artifact_id"] == "EVID-001"
    assert trace["artifact_events"][0]["source_event_type"] == (
        "record_research_artifact"
    )
    assert trace["artifact_events"][0]["payload"]["title"] == (
        "Port authority bulletin"
    )
    assert trace["artifact_warnings"] == []
    assert repo.get_task(task_id).archive_object_key in storage.objects
    _assert_no_browser_leak(trace)


@pytest.mark.asyncio
async def test_tool_evidence_ledger_events_reach_artifacts_and_archive_trace():
    from limira_tools.limira_evidence import ToolEvidenceLedger
    from pipeline_helpers import expand_stream_message

    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    ledger = ToolEvidenceLedger(task_id="runner-task-a")
    runner_events = []
    runner_events.extend(
        expand_stream_message(
            {
                "event": "tool_call",
                "data": {
                    "tool_call_id": "call-search",
                    "tool_name": "google_search",
                    "tool_input": {"q": "BYD 1260H"},
                },
            },
            evidence_ledger=ledger,
        )
    )
    runner_events.extend(
        expand_stream_message(
            {
                "event": "tool_call",
                "data": {
                    "tool_call_id": "call-search",
                    "tool_name": "google_search",
                    "tool_input": {
                        "result": json.dumps(
                            {
                                "organic": [
                                    {
                                        "title": "DoD 1260H List",
                                        "link": "https://example.test/dod-1260h.pdf",
                                        "snippet": "Official list entry summary.",
                                    }
                                ],
                                "searchParameters": {"q": "BYD 1260H"},
                            }
                        )
                    },
                },
            },
            evidence_ledger=ledger,
        )
    )
    research = FakeResearchClient(events=runner_events)

    created = await limira.create_research_task(
        {"query": "BYD 1260H"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    evidence_event = next(
        event for event in events if event["type"] == "evidence_collected"
    )
    assert evidence_event["payload"]["source_event_type"] == "tool_evidence_ledger"
    assert evidence_event["payload"]["source_type"] == "web_search_result"
    assert evidence_event["payload"]["query"] == "BYD 1260H"

    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    evidence = artifacts["evidence"][0]
    assert evidence["evidence_id"].startswith("EVID-")
    assert evidence["title"] == "DoD 1260H List"
    assert evidence["source_url"] == "https://example.test/dod-1260h.pdf"
    assert evidence["summary"] == "Official list entry summary."
    assert evidence["source_event_type"] == "tool_evidence_ledger"

    archive_response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    trace = json.loads(_archive_member_texts(archive_response.body)["trace.json"])

    assert trace["artifacts"]["evidence"][0]["title"] == "DoD 1260H List"
    assert trace["artifact_events"][0]["type"] == "evidence_collected"
    assert trace["artifact_events"][0]["source_event_type"] == "tool_evidence_ledger"
    assert trace["artifact_events"][0]["local_artifact_id"].startswith("EVID-")
    assert trace["artifact_warnings"] == []
    _assert_no_browser_leak(trace)


@pytest.mark.asyncio
async def test_event_proxy_records_runtime_state_to_redis():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(
        redis,
        key_prefix="test:limira:runtime",
        ttl_seconds=120,
    )
    research = FakeResearchClient(
        events=[
            {
                "type": "status",
                "payload": {"status": "running", "archive_status": "pending"},
            },
            {
                "type": "evidence_collected",
                "payload": {"title": "Runtime source"},
            },
            {
                "type": "status",
                "payload": {"status": "completed", "archive_status": "ready"},
            },
        ]
    )

    created = await limira.create_research_task(
        {"query": "redis runtime state"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    key = runtime_state.task_key(task_id)
    runtime_hash = redis.hashes[key]
    assert events[-1]["payload"]["status"] == "completed"
    assert json.loads(runtime_hash["owner_user_id"]) == "user-a"
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"
    assert json.loads(runtime_hash["last_event_type"]) == "status"
    assert key in {call["key"] for call in redis.hset_calls}
    assert redis.expire_calls[-1] == {"key": key, "seconds": 120}


@pytest.mark.asyncio
async def test_event_proxy_ignores_runner_events_after_terminal_status():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        events=[
            {
                "type": "status",
                "payload": {"status": "completed", "archive_status": "ready"},
            },
            {
                "type": "status",
                "payload": {"status": "running", "archive_status": "pending"},
            },
        ]
    )
    created = await limira.create_research_task(
        {"query": "terminal should not regress"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "completed",
                "archive_status": "ready",
                "terminal": True,
            },
        }
    ]
    task = repo.get_task(task_id)
    assert task.status == "completed"
    assert task.archive_status == "ready"
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["last_event"]) == events[-1]
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"


@pytest.mark.asyncio
async def test_event_proxy_cancellation_after_terminal_yield_preserves_completed_state():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        events=[
            {
                "type": "status",
                "payload": {"status": "completed", "archive_status": "ready"},
            },
        ]
    )
    created = await limira.create_research_task(
        {"query": "terminal cancellation must not regress"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    task = repo.get_task(task_id)

    stream = limira._limira_event_stream(
        task,
        user,
        repo,
        research,
        runtime_state,
    )
    first_chunk = await stream.__anext__()
    events = _parse_sse_chunks([first_chunk])

    with pytest.raises(StopAsyncIteration):
        await stream.athrow(asyncio.CancelledError())

    assert events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "completed",
                "archive_status": "ready",
                "terminal": True,
            },
        }
    ]
    persisted_task = repo.get_task(task_id)
    assert persisted_task.status == "completed"
    assert persisted_task.archive_status == "ready"
    assert persisted_task.error is None
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["terminal"]) is True
    assert "error" not in runtime_hash
    assert json.loads(runtime_hash["last_event"]) == events[-1]
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"
    assert research.stream_calls
    assert research.status_calls == []
    _assert_no_browser_leak(events)
    _assert_no_browser_leak(runtime_hash)


@pytest.mark.asyncio
async def test_event_proxy_records_nested_terminal_status_to_redis():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        events=[
            {
                "type": "status",
                "payload": {
                    "data": {"status": "completed", "archive_status": "ready"}
                },
            }
        ]
    )

    created = await limira.create_research_task(
        {"query": "nested terminal status"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"


@pytest.mark.asyncio
async def test_event_proxy_records_authoritative_terminal_status_to_redis():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        events=[],
        status_payload={
            "task_id": "runner-task-a",
            "status": "completed",
            "archive_status": "ready",
        },
    )

    created = await limira.create_research_task(
        {"query": "authoritative terminal status"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["payload"]["status_source"] == "runner"
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"
    assert len(research.status_calls) == 1


@pytest.mark.asyncio
async def test_postgres_event_proxy_authoritative_terminal_status_closes_runtime_stream():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        events=[],
        status_payload={
            "task_id": "runner-task-a",
            "status": "completed",
            "archive_status": "ready",
        },
    )
    created = await limira.create_research_task(
        {"query": "postgres authoritative terminal status"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "completed",
                "archive_status": "ready",
                "terminal": True,
                "status_source": "runner",
            },
        }
    ]
    persisted_task = repo.get_task(task_id)
    assert persisted_task.status == "completed"
    assert persisted_task.archive_status == "ready"
    assert persisted_task.error is None
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["last_event"]) == events[-1]
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"
    assert len(research.stream_calls) == 1
    assert len(research.status_calls) == 1

    reattach_research = FakeResearchClient(
        stream_exception=AssertionError("terminal reattach must not call runner")
    )
    reattach_response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=reattach_research,
        runtime_state=runtime_state,
    )
    reattach_events = _parse_sse_chunks(
        [chunk async for chunk in reattach_response.body_iterator]
    )
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]

    assert reattach_events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "completed",
                "archive_status": "ready",
                "terminal": True,
            },
        }
    ]
    assert repo.get_task(task_id).status == "completed"
    assert repo.get_task(task_id).archive_status == "ready"
    assert reattach_research.stream_calls == []
    assert reattach_research.status_calls == []
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_reattach"
    serialized = json.dumps(
        {
            "events": events,
            "reattach_events": reattach_events,
            "task": (await limira.get_task(task_id, user=user, repo=repo)),
            "runtime": runtime_hash,
        },
        ensure_ascii=False,
        default=str,
    )
    _assert_no_browser_leak(serialized)


@pytest.mark.asyncio
async def test_terminal_task_reattach_records_terminal_runtime_state_to_redis():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        stream_exception=AssertionError("terminal reattach must not stream")
    )
    created = await limira.create_research_task(
        {"query": "already terminal"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.update_task(task_id, status="completed", archive_status="ready")

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["payload"]["terminal"] is True
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_reattach"
    assert research.stream_calls == []


@pytest.mark.asyncio
async def test_terminal_reattach_does_not_close_foreign_active_runtime_lease():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        stream_exception=AssertionError("terminal reattach must not stream")
    )
    created = await limira.create_research_task(
        {"query": "terminal while active stream still owns lease"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    assert await runtime_state.try_open_stream(
        task_id,
        owner_user_id="user-a",
        stream_id="active-stream",
        fields={"status": "running", "archive_status": "pending"},
    )
    repo.update_task(task_id, status="completed", archive_status="ready")

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["payload"]["terminal"] is True
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["stream_state"]) == "open"
    assert json.loads(runtime_hash["stream_id"]) == "active-stream"
    assert not await runtime_state.try_open_stream(
        task_id,
        owner_user_id="user-a",
        stream_id="new-stream",
        fields={"status": "running", "archive_status": "pending"},
    )
    assert research.stream_calls == []


@pytest.mark.asyncio
async def test_event_proxy_duplicate_active_stream_uses_runtime_lease_without_runner_call():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(
        redis,
        key_prefix="test:limira",
        ttl_seconds=240,
    )
    research = FakeResearchClient(
        stream_exception=AssertionError("duplicate stream must not call runner")
    )
    created = await limira.create_research_task(
        {"query": "duplicate stream"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    assert await runtime_state.try_open_stream(
        task_id,
        owner_user_id="user-a",
        stream_id="active-stream",
        fields={"status": "running", "archive_status": "pending"},
    )

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "running",
                "archive_status": "pending",
                "stream_state": "open",
                "status_source": "limira_runtime_state",
                "reason": "stream_already_open",
            },
        }
    ]
    assert research.stream_calls == []
    assert json.loads(runtime_hash["stream_state"]) == "open"
    assert json.loads(runtime_hash["stream_id"]) == "active-stream"
    assert {"key": runtime_state.task_key(task_id), "seconds": 240} in redis.expire_calls


@pytest.mark.asyncio
async def test_event_proxy_cancellation_closes_matching_runtime_stream():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(stream_exception=asyncio.CancelledError())
    created = await limira.create_research_task(
        {"query": "cancelled redis stream"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events == []
    assert json.loads(runtime_hash["status"]) == "cancelled"
    assert json.loads(runtime_hash["archive_status"]) == "failed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "event_stream_cancelled"


@pytest.mark.asyncio
async def test_event_proxy_http_exception_records_failed_terminal_runtime_state():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        stream_exception=HTTPException(status_code=503, detail="runner_unavailable")
    )
    created = await limira.create_research_task(
        {"query": "http exception redis state"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["error"] == "runner_unavailable"
    assert json.loads(runtime_hash["status"]) == "failed"
    assert json.loads(runtime_hash["archive_status"]) == "failed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["error"]) == "runner_unavailable"
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "http_exception"


@pytest.mark.asyncio
async def test_event_proxy_http_exception_hides_internal_detail_from_browser_runtime_and_task_state():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    internal_detail = (
        "runner stream failed at http://10.20.30.40:8091/internal/tasks/runner-task-a/events "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt"
    )
    research = FakeResearchClient(
        stream_exception=HTTPException(status_code=503, detail=internal_detail)
    )
    created = await limira.create_research_task(
        {"query": "http exception internal detail"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    task_payload = await limira.get_task(task_id, user=user, repo=repo)
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]

    assert events[-1]["payload"]["error"] == "limira_event_proxy_failed"
    assert repo.get_task(task_id).error == "limira_event_proxy_failed"
    assert task_payload["error"] == "limira_event_proxy_failed"
    assert json.loads(runtime_hash["error"]) == "limira_event_proxy_failed"
    serialized = json.dumps(
        {
            "events": events,
            "task": task_payload,
            "runtime": runtime_hash,
            "repo_error": repo.get_task(task_id).error,
        },
        ensure_ascii=False,
    )
    for leaked in ("http://10.20.30.40:8091", "limira/users/hash"):
        assert leaked not in serialized
    _assert_no_browser_leak(events)
    _assert_no_browser_leak(task_payload)


@pytest.mark.asyncio
async def test_event_proxy_generic_exception_records_failed_terminal_runtime_state():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(stream_exception=RuntimeError("private failure"))
    created = await limira.create_research_task(
        {"query": "generic exception redis state"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["error"] == "limira_event_proxy_failed"
    assert json.loads(runtime_hash["status"]) == "failed"
    assert json.loads(runtime_hash["archive_status"]) == "failed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["error"]) == "limira_event_proxy_failed"
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "limira_event_proxy_failed"


@pytest.mark.asyncio
async def test_event_proxy_generic_exception_hides_internal_error_from_task_payload():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    internal_error = (
        "stream failed at http://10.20.30.40:8091/internal/tasks/runner-task-a/events "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt "
        "OPENAI_API_KEY=sk-genericinternal123456"
    )
    research = FakeResearchClient(stream_exception=RuntimeError(internal_error))
    created = await limira.create_research_task(
        {"query": "generic exception internal error"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    task_payload = await limira.get_task(task_id, user=user, repo=repo)
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]

    assert events[-1]["payload"]["error"] == "limira_event_proxy_failed"
    assert repo.get_task(task_id).error == "limira_event_proxy_failed"
    assert task_payload["error"] == "limira_event_proxy_failed"
    assert json.loads(runtime_hash["error"]) == "limira_event_proxy_failed"
    serialized = json.dumps(
        {
            "events": events,
            "task": task_payload,
            "repo_error": repo.get_task(task_id).error,
            "runtime": runtime_hash,
        },
        ensure_ascii=False,
    )
    for leaked in (
        "http://10.20.30.40:8091",
        "limira/users/hash",
        "sk-genericinternal123456",
        "OPENAI_API_KEY",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(events)
    _assert_no_browser_leak(task_payload)


@pytest.mark.asyncio
async def test_postgres_event_proxy_preserves_recorded_report_section_after_late_stream_failure():
    class LateFailingResearchClient(FakeResearchClient):
        async def stream_events(self, *, task, user):
            self.stream_calls.append({"task": task, "user": user})
            for event in self.events:
                yield event
            raise RuntimeError(
                "late stream failure at http://10.20.30.40:8091/internal/tasks/"
                "runner-task-a/events for limira/users/hash/tasks/task-a/uploads/doc.txt "
                "OPENAI_API_KEY=sk-latefailureinternal123456"
            )

    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = LateFailingResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "record_research_artifact",
                "payload": {
                    "artifact_type": "report_section",
                    "payload": {
                        "title": "Late failure report",
                        "markdown": "# Late answer\n\nRecorded before failure. [EVID-001]",
                    },
                    "evidence_refs": ["EVID-001"],
                    "confidence": 0.72,
                    "notes": "artifact emitted before runner failure",
                },
            }
        ]
    )
    created = await limira.create_research_task(
        {"query": "late failure artifact persistence"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    task_payload = await limira.get_task(task_id, user=user, repo=repo)
    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    reports = repo.list_task_reports(task_id=task_id)
    trace_events = repo.get_artifact_trace_events(task_id)
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]

    assert [event["type"] for event in events] == [
        "record_research_artifact",
        "error",
    ]
    assert events[-1]["payload"]["error"] == "limira_event_proxy_failed"
    assert task_payload["status"] == "failed"
    assert task_payload["archive_status"] == "failed"
    assert task_payload["error"] == "limira_event_proxy_failed"
    assert json.loads(runtime_hash["status"]) == "failed"
    assert json.loads(runtime_hash["archive_status"]) == "failed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["error"]) == "limira_event_proxy_failed"
    assert json.loads(runtime_hash["stream_close_reason"]) == (
        "limira_event_proxy_failed"
    )

    assert len(artifacts["report_sections"]) == 1
    report_section = artifacts["report_sections"][0]
    assert report_section["section_id"] == "REPORT-001"
    assert report_section["title"] == "Late failure report"
    assert report_section["markdown"].startswith("# Late answer")
    assert report_section["source_event_type"] == "record_research_artifact"
    assert report_section["evidence_refs"] == ["EVID-001"]
    assert report_section["confidence"] == 0.72
    assert [report.report_id for report in reports] == ["REPORT-001"]
    assert reports[0].markdown == report_section["markdown"]
    assert trace_events[0]["type"] == "report_section_generated"
    assert trace_events[0]["source_event_type"] == "record_research_artifact"
    assert trace_events[0]["local_artifact_id"] == "REPORT-001"

    serialized = json.dumps(
        {
            "events": events,
            "task": task_payload,
            "artifacts": artifacts,
            "reports": [report.public_dict() for report in reports],
            "trace_events": trace_events,
            "runtime": runtime_hash,
        },
        ensure_ascii=False,
        default=str,
    )
    for leaked in (
        "http://10.20.30.40:8091",
        "limira/users/hash",
        "sk-latefailureinternal123456",
        "OPENAI_API_KEY",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(serialized)


@pytest.mark.asyncio
async def test_postgres_event_proxy_preserves_recorded_report_section_after_late_stream_cancellation():
    class LateCancellingResearchClient(FakeResearchClient):
        async def stream_events(self, *, task, user):
            self.stream_calls.append({"task": task, "user": user})
            for event in self.events:
                yield event
            raise asyncio.CancelledError()

    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = LateCancellingResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "record_research_artifact",
                "payload": {
                    "artifact_type": "report_section",
                    "payload": {
                        "title": "Late cancellation report",
                        "markdown": "# Cancelled answer\n\nRecorded before cancel. [EVID-001]",
                    },
                    "evidence_refs": ["EVID-001"],
                    "confidence": 0.68,
                    "notes": "artifact emitted before stream cancellation",
                },
            }
        ]
    )
    created = await limira.create_research_task(
        {"query": "late cancellation artifact persistence"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    task_payload = await limira.get_task(task_id, user=user, repo=repo)
    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    reports = repo.list_task_reports(task_id=task_id)
    trace_events = repo.get_artifact_trace_events(task_id)
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    last_event = json.loads(runtime_hash["last_event"])

    assert [event["type"] for event in events] == ["record_research_artifact"]
    assert task_payload["status"] == "cancelled"
    assert task_payload["archive_status"] == "failed"
    assert task_payload["error"] == "event_stream_cancelled"
    assert repo.get_task(task_id).status == "cancelled"
    assert repo.get_task(task_id).error == "event_stream_cancelled"
    assert json.loads(runtime_hash["status"]) == "cancelled"
    assert json.loads(runtime_hash["archive_status"]) == "failed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["error"]) == "event_stream_cancelled"
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "event_stream_cancelled"
    assert last_event["type"] == "record_research_artifact"

    assert len(artifacts["report_sections"]) == 1
    report_section = artifacts["report_sections"][0]
    assert report_section["section_id"] == "REPORT-001"
    assert report_section["title"] == "Late cancellation report"
    assert report_section["markdown"].startswith("# Cancelled answer")
    assert report_section["source_event_type"] == "record_research_artifact"
    assert report_section["evidence_refs"] == ["EVID-001"]
    assert report_section["confidence"] == 0.68
    assert [report.report_id for report in reports] == ["REPORT-001"]
    assert reports[0].markdown == report_section["markdown"]
    assert trace_events[0]["type"] == "report_section_generated"
    assert trace_events[0]["source_event_type"] == "record_research_artifact"
    assert trace_events[0]["local_artifact_id"] == "REPORT-001"

    serialized = json.dumps(
        {
            "events": events,
            "task": task_payload,
            "artifacts": artifacts,
            "reports": [report.public_dict() for report in reports],
            "trace_events": trace_events,
            "runtime": runtime_hash,
            "last_event": last_event,
        },
        ensure_ascii=False,
        default=str,
    )
    _assert_no_browser_leak(serialized)


@pytest.mark.asyncio
async def test_completed_task_event_reattach_is_terminal_and_does_not_call_runner():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    research = FakeResearchClient(
        stream_exception=AssertionError("final reattach must not call runner stream")
    )
    created = await limira.create_research_task(
        {"query": "completed query"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.update_task(task_id, status="completed", archive_status="ready")

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "completed",
                "archive_status": "ready",
                "terminal": True,
            },
        }
    ]
    assert research.stream_calls == []
    assert research.status_calls == []
    assert repo.get_task(task_id).status == "completed"
    assert repo.get_task(task_id).archive_status == "ready"


@pytest.mark.asyncio
async def test_postgres_terminal_reattach_preserves_persisted_final_summary_surfaces():
    engine = FakeLimiraPostgresEngine()
    repo = limira.PostgresLimiraTaskRepository(
        "postgresql://limira:test@postgres:5432/limira",
        engine_factory=lambda _url: engine,
    )
    storage = limira.InMemoryLimiraObjectStorage()
    user = limira.LimiraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limira.RedisLimiraRuntimeState(redis, key_prefix="test:limira")
    research = FakeResearchClient(
        stream_exception=AssertionError("terminal final-answer reattach must not stream")
    )
    created = await limira.create_research_task(
        {"query": "completed final summary query"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.record_artifact(
        task_id,
        "report_section",
        {
            "section_id": "REPORT-001",
            "title": "最终回答",
            "markdown": "# Final answer\n\nPersisted answer survives reattach. [EVID-001]",
            "evidence_refs": ["EVID-001"],
            "source_event_type": "final_summary_show_text",
        },
    )
    repo.update_task(task_id, status="completed", archive_status="ready")

    before_reports = repo.list_task_reports(task_id=task_id)
    status_payload = await limira.get_task(task_id, user=user, repo=repo)
    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    artifacts = await limira.get_task_artifacts(task_id, user=user, repo=repo)
    after_reports = repo.list_task_reports(task_id=task_id)
    archive_response = await limira.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    members = _archive_member_texts(archive_response.body)
    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]

    assert status_payload["status"] == "completed"
    assert status_payload["archive_status"] == "ready"
    assert events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "completed",
                "archive_status": "ready",
                "terminal": True,
            },
        }
    ]
    assert research.stream_calls == []
    assert research.status_calls == []
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_reattach"

    assert [report.report_id for report in before_reports] == ["REPORT-001"]
    assert [report.report_id for report in after_reports] == ["REPORT-001"]
    assert after_reports[0].markdown == before_reports[0].markdown
    assert after_reports[0].markdown.startswith("# Final answer")
    assert len(artifacts["report_sections"]) == 1
    assert artifacts["report_sections"][0]["section_id"] == "REPORT-001"
    assert artifacts["report_sections"][0]["markdown"] == after_reports[0].markdown
    assert artifacts["report_sections"][0]["source_event_type"] == (
        "final_summary_show_text"
    )
    assert "# Final answer" in members["report.md"]
    assert "Persisted answer survives reattach" in members["report.md"]
    trace = json.loads(members["trace.json"])
    assert trace["reports"][0]["report_id"] == "REPORT-001"
    assert trace["artifact_events"][0]["type"] == "report_section_generated"
    assert trace["artifact_events"][0]["source_event_type"] == (
        "final_summary_show_text"
    )
    assert repo.get_task(task_id).status == "completed"
    assert repo.get_task(task_id).archive_status == "ready"
    _assert_no_browser_leak(
        {
            "status": status_payload,
            "events": events,
            "artifacts": artifacts,
            "archive": members,
        }
    )


@pytest.mark.asyncio
async def test_event_proxy_runner_conflict_hides_internal_reason_from_browser_runtime_state():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    runtime_state = limira.InMemoryLimiraRuntimeState()
    internal_reason = (
        "task conflict at http://10.20.30.40:8091/internal/tasks/runner-task-a "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt"
    )
    research = FakeResearchClient(
        stream_exception=limira.RunnerStreamConflict(internal_reason),
        status_payload={
            "task_id": "runner-task-a",
            "status": "running",
            "archive_status": "pending",
        },
    )
    created = await limira.create_research_task(
        {"query": "conflict internal reason"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    runtime_snapshot = await runtime_state.get_task_runtime(task_id)

    assert events[-1]["payload"]["reason"] == "runner_stream_conflict"
    assert runtime_snapshot["last_event"]["payload"]["reason"] == "runner_stream_conflict"
    serialized = json.dumps(
        {"events": events, "runtime": runtime_snapshot},
        ensure_ascii=False,
    )
    for leaked in ("http://10.20.30.40:8091", "limira/users/hash"):
        assert leaked not in serialized
    _assert_no_browser_leak(events)


@pytest.mark.asyncio
async def test_event_proxy_runner_status_warning_hides_internal_detail_from_browser_runtime_state():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    runtime_state = limira.InMemoryLimiraRuntimeState()
    internal_detail = (
        "runner status failed at http://10.20.30.40:8091/internal/tasks/runner-task-a "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt"
    )

    class StatusWarningResearchClient(FakeResearchClient):
        async def get_task_status(self, *, task, user):
            self.status_calls.append({"task": task, "user": user})
            raise HTTPException(status_code=502, detail=internal_detail)

    research = StatusWarningResearchClient(events=[])
    created = await limira.create_research_task(
        {"query": "warning internal detail"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    runtime_snapshot = await runtime_state.get_task_runtime(task_id)

    assert events[-1]["payload"]["warning"] == "runner_status_warning"
    assert runtime_snapshot["last_warning"] == "runner_status_warning"
    assert runtime_snapshot["last_event"]["payload"]["warning"] == "runner_status_warning"
    serialized = json.dumps(
        {"events": events, "runtime": runtime_snapshot},
        ensure_ascii=False,
    )
    for leaked in ("http://10.20.30.40:8091", "limira/users/hash"):
        assert leaked not in serialized
    _assert_no_browser_leak(events)


@pytest.mark.asyncio
async def test_event_proxy_terminal_reason_hides_internal_conflict_detail_from_browser_runtime_state():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    runtime_state = limira.InMemoryLimiraRuntimeState()
    internal_reason = (
        "terminal conflict at http://10.20.30.40:8091/internal/tasks/runner-task-a "
        "for limira/users/hash/tasks/task-a/uploads/doc.txt"
    )

    class TerminalReasonResearchClient(FakeResearchClient):
        async def get_task_status(self, *, task, user):
            self.status_calls.append({"task": task, "user": user})
            repo.update_task(
                task.task_id,
                status="failed",
                archive_status="failed",
                error="runner_task_failed",
            )
            raise HTTPException(status_code=409, detail="runner_terminal_conflict")

    research = TerminalReasonResearchClient(
        stream_exception=limira.RunnerStreamConflict(internal_reason)
    )
    created = await limira.create_research_task(
        {"query": "terminal internal reason"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])
    runtime_snapshot = await runtime_state.get_task_runtime(task_id)

    assert events[-1]["payload"]["reason"] == "runner_stream_conflict"
    assert events[-1]["payload"]["error"] == "runner_task_failed"
    assert runtime_snapshot["last_event"]["payload"]["reason"] == "runner_stream_conflict"
    serialized = json.dumps(
        {"events": events, "runtime": runtime_snapshot},
        ensure_ascii=False,
    )
    for leaked in ("http://10.20.30.40:8091", "limira/users/hash"):
        assert leaked not in serialized
    _assert_no_browser_leak(events)


@pytest.mark.asyncio
async def test_eventsource_reconnect_after_completion_does_not_regress_task():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    research = FakeResearchClient(
        events=[
            {
                "type": "status",
                "payload": {"status": "completed", "archive_status": "ready"},
            }
        ],
        stream_exception=None,
    )
    created = await limira.create_research_task(
        {"query": "finish once"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    first_response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    first_events = _parse_sse_chunks([chunk async for chunk in first_response.body_iterator])

    second_response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    second_events = _parse_sse_chunks([chunk async for chunk in second_response.body_iterator])

    assert first_events[-1]["payload"]["status"] == "completed"
    assert second_events[-1]["payload"]["status"] == "completed"
    assert second_events[-1]["payload"]["terminal"] is True
    assert len(research.stream_calls) == 1
    assert repo.get_task(task_id).status == "completed"
    assert repo.get_task(task_id).archive_status == "ready"


@pytest.mark.asyncio
async def test_runner_stream_conflict_uses_authoritative_status_instead_of_failing():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    research = FakeResearchClient(
        stream_exception=limira.RunnerStreamConflict("task_already_finished"),
        status_payload={
            "task_id": "runner-task-a",
            "status": "completed",
            "archive_status": "ready",
        },
    )
    created = await limira.create_research_task(
        {"query": "reattach via runner conflict"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events[-1]["type"] == "status"
    assert events[-1]["payload"]["status"] == "completed"
    assert events[-1]["payload"]["archive_status"] == "ready"
    assert events[-1]["payload"]["reason"] == "task_already_finished"
    assert repo.get_task(task_id).status == "completed"
    assert repo.get_task(task_id).archive_status == "ready"
    assert repo.get_task(task_id).error is None


@pytest.mark.asyncio
async def test_runner_stream_running_conflict_stays_running_without_failed_regression():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    research = FakeResearchClient(
        stream_exception=limira.RunnerStreamConflict("task_already_running"),
        status_payload={
            "task_id": "runner-task-a",
            "status": "running",
            "archive_status": "pending",
        },
    )
    created = await limira.create_research_task(
        {"query": "active duplicate stream"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events[-1]["payload"]["status"] == "running"
    assert events[-1]["payload"]["archive_status"] == "pending"
    assert events[-1]["payload"]["reason"] == "task_already_running"
    assert repo.get_task(task_id).status == "running"
    assert repo.get_task(task_id).archive_status == "pending"
    assert repo.get_task(task_id).error is None


@pytest.mark.asyncio
async def test_event_proxy_cancellation_does_not_mark_task_completed():
    repo = limira.InMemoryLimiraTaskRepository()
    user = limira.LimiraUser("user-a")
    research = FakeResearchClient(stream_exception=asyncio.CancelledError())
    created = await limira.create_research_task(
        {"query": "cancelled stream"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limira.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events == []
    assert repo.get_task(task_id).status == "cancelled"
    assert repo.get_task(task_id).archive_status == "failed"
    assert repo.get_task(task_id).error == "event_stream_cancelled"


@pytest.mark.asyncio
async def test_event_proxy_keeps_artifacts_user_scoped():
    repo = limira.InMemoryLimiraTaskRepository()
    user_a = limira.LimiraUser("user-a")
    user_b = limira.LimiraUser("user-b")
    research = FakeResearchClient(
        events=[
            {
                "type": "evidence_collected",
                "payload": {"evidence_id": "EVID-777", "title": "private"},
            }
        ]
    )

    created = await limira.create_research_task(
        {"query": "private query"},
        request=None,
        user=user_a,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    response = await limira.get_task_events(
        task_id,
        user=user_a,
        repo=repo,
        research_client=research,
        runtime_state=limira.InMemoryLimiraRuntimeState(),
    )
    _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    with pytest.raises(HTTPException) as forbidden:
        await limira.get_task_artifacts(task_id, user=user_b, repo=repo)
    assert forbidden.value.status_code == 404


def test_runner_research_client_uses_server_side_headers(monkeypatch):
    monkeypatch.setenv("LIMIRA_RUNNER_INTERNAL_URL", "http://internal-runner")
    monkeypatch.setenv("LIMIRA_RUNNER_SERVICE_TOKEN", "server-only-token")

    client = limira.RunnerResearchClient()
    assert client.runner_url == "http://internal-runner"
    assert client.service_token == "server-only-token"


def test_limira_router_defines_required_browser_facing_paths():
    route_contract = {
        ("/auth/session", "GET"),
        ("/auth/signin", "POST"),
        ("/auth/signout", "POST"),
        ("/auth/signup", "POST"),
        ("/auth/verify-email", "POST"),
        ("/auth/resend-verification", "POST"),
        ("/auth/password-reset/request", "POST"),
        ("/auth/password-reset/confirm", "POST"),
        ("/auth/organizations", "GET"),
        ("/auth/enterprise/signin", "POST"),
        ("/auth/google/config", "GET"),
        ("/auth/google/start", "GET"),
        ("/auth/google/callback", "GET"),
        ("/auth/wechat/config", "GET"),
        ("/auth/wechat/start", "GET"),
        ("/auth/wechat/callback", "GET"),
        ("/scenarios", "GET"),
        ("/research", "POST"),
        ("/tasks/{task_id}", "GET"),
        ("/tasks/{task_id}/events", "GET"),
        ("/tasks/{task_id}/artifacts", "GET"),
        ("/tasks/{task_id}/archive.zip", "GET"),
        ("/uploads", "GET"),
        ("/uploads", "POST"),
        ("/uploads/search", "GET"),
        ("/uploads/{document_id}", "GET"),
        ("/uploads/{document_id}/download", "GET"),
        ("/tasks/{task_id}/reports/pdf", "POST"),
        ("/tasks/{task_id}/reports/{report_id}/pdf", "GET"),
        ("/enterprise/members", "GET"),
        ("/enterprise/members", "POST"),
        ("/enterprise/usage", "GET"),
        ("/admin/organizations", "POST"),
        ("/admin/tasks/{task_id}", "GET"),
        ("/admin/tasks/{task_id}/event-logs", "GET"),
        ("/admin/tasks/{task_id}/archive.zip", "GET"),
    }
    actual = {
        (route.path, method)
        for route in limira.router.routes
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST"}
    }

    assert route_contract <= actual
    for path, _method in actual:
        assert "/limira-runner/" not in path


def _archive_zip(*, extra_member: bool = False, secret_members: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if secret_members:
            archive.writestr(
                "metadata.json",
                json.dumps(
                    {
                        "Authorization": "Bearer runner-token-123456",
                        "cookie": "legacy_session=session-secret-123456",
                        "url": "https://search.test?q=x&token=archive-token-123456",
                    }
                ),
            )
            archive.writestr(
                "report.html",
                "<!doctype html><main>OPENAI_API_KEY=sk-archiveopenai123456</main>",
            )
            archive.writestr(
                "report.md",
                "# report\nRUNNER_SERVICE_TOKEN=archive-runner-token-123456\n"
                "https://api.test/resource?api_key=archive-query-secret-123456",
            )
            archive.writestr(
                "trace.json",
                json.dumps(
                    {
                        "headers": {"Cookie": "trace-cookie-secret-123456"},
                        "deepseek": "DEEPSEEK_API_KEY=sk-tracedeepseek123456",
                        "jwt": "eyJtrace.secret.payload",
                    }
                ),
            )
        else:
            archive.writestr("metadata.json", "{}")
            archive.writestr("report.html", "<!doctype html><main></main>")
            archive.writestr("report.md", "# report")
            archive.writestr("trace.json", "{}")
        if extra_member:
            archive.writestr(".env", "RUNNER_SERVICE_TOKEN=secret")
    return buffer.getvalue()


def _archive_zip_with_json_wrapped_report(
    *,
    task_id: str,
    owner_user_id: str,
    report_text: str,
) -> bytes:
    trace = {
        "task": {
            "task_id": task_id,
            "owner_user_id": owner_user_id,
            "query": "BYD 1260H",
            "status": "completed",
            "archive_status": "ready",
            "scenario": None,
            "error": None,
            "model_summary": {},
        },
        "artifacts": {
            "report_sections": [
                {
                    "section_id": "REPORT-007",
                    "markdown": report_text,
                    "source_event_type": "record_research_artifact",
                }
            ]
        },
        "artifact_events": [
            {
                "type": "report_section_generated",
                "artifact_type": "report_section",
                "bucket": "report_sections",
                "local_artifact_id": "REPORT-007",
                "source_event_type": "record_research_artifact",
                "payload": {
                    "section_id": "REPORT-007",
                    "markdown": report_text,
                },
            }
        ],
        "artifact_warnings": [],
        "reports": [],
        "uploaded_documents": [],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("metadata.json", json.dumps({"task": trace["task"]}))
        archive.writestr("report.html", f"<!doctype html><main>{report_text}</main>")
        archive.writestr("report.md", report_text)
        archive.writestr("trace.json", json.dumps(trace, ensure_ascii=False))
    return buffer.getvalue()


def _archive_zip_with_raw_model_summary(
    *,
    task_id: str,
    owner_user_id: str,
    runner_task_id: str = "runner-task-secret",
) -> bytes:
    task_payload = {
        "task_id": task_id,
        "owner_user_id": owner_user_id,
        "query": "archive internal state reuse",
        "status": "completed",
        "archive_status": "ready",
        "scenario": None,
        "error": None,
        "model_summary": _internal_model_summary(
            task_id,
            runner_task_id=runner_task_id,
        ),
    }
    metadata = {
        "task": task_payload,
        "artifact_counts": {},
        "artifact_event_count": 0,
        "artifact_warning_count": 0,
        "reports": [],
        "uploaded_documents": [],
    }
    trace = {
        "task": dict(task_payload),
        "artifacts": {},
        "artifact_events": [],
        "artifact_warnings": [],
        "reports": [],
        "uploaded_documents": [],
    }
    members = {
        "metadata.json": json.dumps(metadata, ensure_ascii=False),
        "report.html": "<!doctype html><main>safe report</main>",
        "report.md": "# safe report",
        "trace.json": json.dumps(trace, ensure_ascii=False),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member_name in ("metadata.json", "report.html", "report.md", "trace.json"):
            archive.writestr(member_name, members[member_name])
    return buffer.getvalue()


def _archive_zip_with_undecodable_report_member() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("metadata.json", "{}")
        archive.writestr(
            "report.html",
            b"\xff\xfeOPENAI_API_KEY=sk-binarysecret123456",
        )
        archive.writestr("report.md", "# stale report")
        archive.writestr("trace.json", "{}")
    return buffer.getvalue()


def _archive_zip_with_invalid_json_metadata_members(task_id: str) -> bytes:
    buffer = io.BytesIO()
    internal_metadata = (
        "runner_task_id=runner-invalid-json-123\n"
        "url=http://10.20.30.40:8091/limira-runner/tasks/runner-invalid-json-123\n"
        f"object_key=limira/users/hash/tasks/{task_id}/archives/archive.zip\n"
    )
    internal_trace = (
        "archive_object_key="
        f"limira/users/hash/tasks/{task_id}/archives/archive.zip\n"
        "runner_task_id=runner-invalid-json-123\n"
    )
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("metadata.json", internal_metadata)
        archive.writestr("report.html", "<!doctype html><main>safe report</main>")
        archive.writestr("report.md", "# safe report")
        archive.writestr("trace.json", internal_trace)
    return buffer.getvalue()


def _archive_member_texts(archive_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        return {
            member_name: archive.read(member_name).decode("utf-8")
            for member_name in archive.namelist()
        }


def _internal_model_summary(
    task_id: str,
    *,
    runner_task_id: str = "runner-task-secret",
) -> dict[str, object]:
    return {
        "provider": "deepseek",
        "runner_task_id": runner_task_id,
        "object_key": f"limira/users/hash/tasks/{task_id}/uploads/doc.txt",
        "endpoint": f"http://10.20.30.40:8091/limira-runner/tasks/{runner_task_id}",
        "nested": {
            "archive_object_key": (
                f"limira/users/hash/tasks/{task_id}/archives/archive.zip"
            ),
            "safe": "kept",
            "warning": f"limira/users/hash/tasks/{task_id}/uploads/doc.txt",
        },
    }


def _assert_archive_hides_internal_model_summary_identifiers(
    archive_bytes: bytes,
) -> tuple[dict[str, object], dict[str, object]]:
    members = _archive_member_texts(archive_bytes)
    metadata = json.loads(members["metadata.json"])
    trace = json.loads(members["trace.json"])
    _assert_public_archive_hides_internal_identifiers(archive_bytes)

    for task_payload in (metadata["task"], trace["task"]):
        model_summary = task_payload["model_summary"]
        assert model_summary["provider"] == "deepseek"
        assert model_summary["nested"]["safe"] == "kept"
        assert model_summary["endpoint"] == "limira_internal_value_redacted"
        assert model_summary["nested"]["warning"] == "limira_internal_value_redacted"

    serialized = json.dumps(
        {
            "metadata": metadata,
            "trace": trace,
        },
        ensure_ascii=False,
    )
    for leaked in (
        "owner_user_id",
        "runner_task_id",
        "runner-task-secret",
        "object_key",
        "archive_object_key",
        "limira/users/hash",
        "http://10.20.30.40:8091",
        "/limira-runner/",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(serialized)
    return metadata, trace


def _assert_public_archive_hides_internal_identifiers(archive_bytes: bytes) -> None:
    members = _archive_member_texts(archive_bytes)
    metadata = json.loads(members["metadata.json"])
    trace = json.loads(members["trace.json"])

    for task_payload in (metadata.get("task") or {}, trace.get("task") or {}):
        for internal_field in (
            "owner_user_id",
            "runner_task_id",
            "object_key",
            "archive_object_key",
            "pdf_object_key",
        ):
            assert internal_field not in task_payload

    serialized = json.dumps(
        {
            "metadata": metadata,
            "trace": trace,
        },
        ensure_ascii=False,
    )
    for leaked in (
        "owner_user_id",
        "runner_task_id",
        "object_key",
        "archive_object_key",
        "pdf_object_key",
        "limira/users/",
        "/limira-runner/",
    ):
        assert leaked not in serialized


def _assert_archive_hides_invalid_json_member_identifiers(archive_bytes: bytes) -> None:
    members = _archive_member_texts(archive_bytes)
    json.loads(members["metadata.json"])
    json.loads(members["trace.json"])
    serialized = json.dumps(members, ensure_ascii=False)
    for leaked in (
        "/limira-runner/",
        "runner_task_id",
        "runner-invalid-json-123",
        "object_key",
        "archive_object_key",
        "limira/users/",
        "http://10.20.30.40:8091",
    ):
        assert leaked not in serialized
    _assert_no_browser_leak(serialized)


def _parse_sse_chunks(chunks):
    text = b"".join(
        chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
        for chunk in chunks
    ).decode("utf-8")
    return [
        json.loads(line.removeprefix("data: "))
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


def _assert_no_browser_leak(payload):
    text = str(payload)
    for forbidden in limira.FORBIDDEN_BROWSER_SUBSTRINGS:
        assert forbidden not in text


def _assert_no_raw_secret(payload):
    text = (
        payload.decode("utf-8", errors="ignore")
        if isinstance(payload, bytes)
        else json.dumps(payload, ensure_ascii=False)
        if isinstance(payload, (dict, list))
        else str(payload)
    )
    for forbidden in [
        "runner-token-123456",
        "session-secret-123456",
        "archive-token-123456",
        "sk-archiveopenai123456",
        "archive-runner-token-123456",
        "archive-query-secret-123456",
        "trace-cookie-secret-123456",
        "sk-tracedeepseek123456",
        "eyJtrace.secret.payload",
        "report-bearer-secret-123456",
        "sk-reportopenai123456",
        "serper-secret-123456",
        "report-cookie-secret-123456",
        "report-url-token-123456",
        "nested-jina-secret-123456",
    ]:
        assert forbidden not in text


def _limira_asgi_app():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    pdf_exporter = FakePdfExporter()
    app = FastAPI()
    app.include_router(limira.router, prefix="/api/limira")
    app.state.test_pdf_exporter = pdf_exporter

    async def current_user_override():
        return limira.LimiraUser("user-a")

    async def task_repository_override():
        return repo

    async def object_storage_override():
        return storage

    async def pdf_exporter_override():
        return pdf_exporter

    app.dependency_overrides[limira.get_current_limira_user] = current_user_override
    app.dependency_overrides[limira.get_task_repository] = task_repository_override
    app.dependency_overrides[limira.get_object_storage] = object_storage_override
    app.dependency_overrides[limira.get_pdf_exporter] = pdf_exporter_override
    return app, repo, storage


class FakeRedisClient:
    def __init__(self):
        self.hashes = {}
        self.hset_calls = []
        self.expire_calls = []
        self.eval_calls = []

    async def hset(self, key, *, mapping):
        self.hset_calls.append({"key": key, "mapping": dict(mapping)})
        self.hashes.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, seconds):
        self.expire_calls.append({"key": key, "seconds": seconds})
        return True

    async def eval(self, script, numkeys, *keys_and_args):
        self.eval_calls.append(
            {
                "script": script,
                "numkeys": numkeys,
                "keys_and_args": keys_and_args,
            }
        )
        assert numkeys == 1
        key = keys_and_args[0]
        ttl_seconds = int(keys_and_args[1])
        if "limira_try_open_stream" in script:
            runtime_hash = self.hashes.setdefault(key, {})
            if runtime_hash.get("stream_state") == json.dumps("open"):
                return 0
            mapping = _pairs_to_mapping(keys_and_args[2:])
            runtime_hash.update(mapping)
            await self.expire(key, ttl_seconds)
            return 1
        if "limira_close_stream" in script:
            runtime_hash = self.hashes.setdefault(key, {})
            expected_stream_id = keys_and_args[2]
            current_stream_id = runtime_hash.get("stream_id")
            if current_stream_id and current_stream_id != expected_stream_id:
                return 0
            mapping = _pairs_to_mapping(keys_and_args[3:])
            runtime_hash.update(mapping)
            await self.expire(key, ttl_seconds)
            return 1
        raise AssertionError(f"unexpected redis eval script: {script}")


class FakeS3Client:
    def __init__(self):
        self.put_calls = []
        self.get_calls = []
        self.objects = {}

    def put_object(self, **kwargs):
        self.put_calls.append(dict(kwargs))
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = bytes(kwargs["Body"])
        return {"ETag": '"fake"'}

    def get_object(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        data = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {"Body": FakeS3Body(data)}


class FakeS3Body:
    def __init__(self, data):
        self.data = bytes(data)

    def read(self):
        return self.data


class FakePdfExporter:
    def __init__(self):
        self.pdf_bytes = b"%PDF-1.7\nfake limira report\n%%EOF"
        self.html_inputs = []

    async def render_pdf(self, html_content):
        self.html_inputs.append(html_content)
        return self.pdf_bytes


class FakeUploadEmbeddingProvider:
    def __init__(self, embedding):
        self.embedding = list(embedding)
        self.calls = []

    async def embed_upload_text(self, text, *, config):
        self.calls.append({"text": text, "config": config})
        return list(self.embedding)


class FakeFailingUploadEmbeddingProvider:
    def __init__(self, error):
        self.error = error
        self.calls = []

    async def embed_upload_text(self, text, *, config):
        self.calls.append({"text": text, "config": config})
        raise self.error


def _pairs_to_mapping(values):
    assert len(values) % 2 == 0
    return {values[index]: values[index + 1] for index in range(0, len(values), 2)}


class FakeLimiraPostgresEngine:
    def __init__(self):
        self.tasks = {}
        self.artifact_events = {}
        self.artifact_trace_events = []
        self.task_event_logs = []
        self.uploaded_documents = {}
        self.vector_search_calls = []
        self.generated_reports = {}
        self.typed_inserts = {
            "limira_evidence_items": [],
            "limira_entities": [],
            "limira_entity_relations": [],
            "limira_timeline_events": [],
            "limira_generated_reports": [],
        }

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params):
        sql = str(statement).lower()
        if "insert into limira_research_tasks" in sql:
            row = {
                "task_id": params["task_id"],
                "owner_user_id": params["owner_user_id"],
                "query": params["query"],
                "status": "queued",
                "archive_status": "pending",
                "runner_task_id": params["runner_task_id"],
                "archive_object_key": None,
                "archive_zip_sha256": None,
                "scenario": params["scenario"],
                "error": None,
                "model_summary": {},
            }
            self.tasks[params["task_id"]] = row
            return FakeLimiraPostgresResult([row])

        if "select owner_user_id from limira_research_tasks" in sql:
            row = self.tasks.get(params["task_id"])
            return FakeLimiraPostgresResult(
                [{"owner_user_id": row["owner_user_id"]}] if row else []
            )

        if "update limira_research_tasks" in sql:
            row = self.tasks.get(params["task_id"])
            if not row:
                return FakeLimiraPostgresResult([])
            for key, value in params.items():
                if key != "task_id":
                    row[key] = value
            return FakeLimiraPostgresResult([row])

        if "from limira_research_tasks" in sql and "limit :limit" in sql:
            rows = [
                row
                for row in reversed(list(self.tasks.values()))
                if row["owner_user_id"] == params["owner_user_id"]
            ]
            return FakeLimiraPostgresResult(rows[: params["limit"]])

        if "from limira_research_tasks" in sql:
            row = self.tasks.get(params["task_id"])
            if row and params.get("owner_user_id") and row["owner_user_id"] != params["owner_user_id"]:
                row = None
            return FakeLimiraPostgresResult([row] if row else [])

        if "insert into limira_artifact_events" in sql:
            key = (
                params["task_id"],
                params["artifact_type"],
                params["local_artifact_id"],
            )
            self.artifact_events[key] = {
                "artifact_type": params["artifact_type"],
                "payload": params["payload"],
            }
            return FakeLimiraPostgresResult([])

        if "insert into limira_artifact_trace_events" in sql:
            self.artifact_trace_events.append(
                {
                    "task_id": params["task_id"],
                    "event_type": params["event_type"],
                    "artifact_type": params["artifact_type"],
                    "bucket": params["bucket"],
                    "local_artifact_id": params["local_artifact_id"],
                    "payload": params["payload"],
                    "source_event_type": params["source_event_type"],
                }
            )
            return FakeLimiraPostgresResult([])

        if "from limira_artifact_trace_events" in sql:
            rows = [
                {
                    "event_type": row["event_type"],
                    "artifact_type": row["artifact_type"],
                    "bucket": row["bucket"],
                    "local_artifact_id": row["local_artifact_id"],
                    "payload": row["payload"],
                    "source_event_type": row["source_event_type"],
                }
                for row in self.artifact_trace_events
                if row["task_id"] == params["task_id"]
            ]
            return FakeLimiraPostgresResult(rows)

        if "from limira_artifact_events" in sql:
            rows = [
                row
                for (task_id, _artifact_type, _local_id), row in self.artifact_events.items()
                if task_id == params["task_id"]
            ]
            return FakeLimiraPostgresResult(rows)

        if "insert into limira_task_event_logs" in sql:
            self.task_event_logs.append(
                {
                    "event_log_id": f"event-log-{len(self.task_event_logs) + 1}",
                    "task_id": params["task_id"],
                    "event_type": params["event_type"],
                    "source": params["source"],
                    "payload": params["payload"],
                    "created_at": f"2026-06-09T00:00:{len(self.task_event_logs):02d}+00:00",
                }
            )
            return FakeLimiraPostgresResult([])

        if "from limira_task_event_logs" in sql:
            rows = [
                row
                for row in reversed(self.task_event_logs)
                if row["task_id"] == params["task_id"]
            ][: params["limit"]]
            return FakeLimiraPostgresResult(rows)

        if "insert into limira_uploaded_documents" in sql:
            row = {
                "document_id": params["document_id"],
                "task_id": params["task_id"],
                "owner_user_id": params["owner_user_id"],
                "original_filename": params["original_filename"],
                "content_type": params["content_type"],
                "byte_size": params["byte_size"],
                "minio_bucket": params["minio_bucket"],
                "object_key": params["object_key"],
                "extracted_text": params["extracted_text"],
                "language": params["language"],
                "embedding": params["embedding"],
                "metadata": params["metadata"],
            }
            self.uploaded_documents[params["document_id"]] = row
            return FakeLimiraPostgresResult([row])

        if "from limira_uploaded_documents" in sql:
            if "embedding <=>" in sql:
                self.vector_search_calls.append(dict(params))
                query_embedding = limira._embedding_from_value(params["query_embedding"])
                rows = []
                for row in self.uploaded_documents.values():
                    if row["owner_user_id"] != params["owner_user_id"]:
                        continue
                    if (
                        params.get("task_id") is not None
                        and row["task_id"] != params["task_id"]
                    ):
                        continue
                    row_embedding = limira._embedding_from_value(row.get("embedding"))
                    score = limira._cosine_similarity(
                        query_embedding or [],
                        row_embedding,
                    )
                    if score is None:
                        continue
                    result_row = dict(row)
                    result_row["limira_search_score"] = score
                    rows.append(result_row)
                rows.sort(
                    key=lambda row: (
                        -row["limira_search_score"],
                        row["original_filename"].lower(),
                        row["document_id"],
                    )
                )
                return FakeLimiraPostgresResult(rows[: params["limit"]])

            if "document_id = :document_id" in sql:
                row = self.uploaded_documents.get(params["document_id"])
                if row and row["owner_user_id"] != params["owner_user_id"]:
                    row = None
                return FakeLimiraPostgresResult([row] if row else [])

            rows = [
                row
                for row in self.uploaded_documents.values()
                if row["owner_user_id"] == params["owner_user_id"]
                and (params.get("task_id") is None or row["task_id"] == params["task_id"])
            ]
            return FakeLimiraPostgresResult(rows)

        if "insert into limira_generated_reports" in sql:
            row = {
                "report_id": params["report_id"],
                "task_id": params["task_id"],
                "report_type": params.get("report_type") or "section",
                "markdown": params["markdown"],
                "html": params["html"],
                "pdf_object_key": params.get("pdf_object_key"),
                "evidence_refs": params["evidence_refs"],
                "creator_user_id": params["creator_user_id"],
                "metadata": params["metadata"],
            }
            self.generated_reports[(params["task_id"], params["report_id"])] = row
            self.typed_inserts["limira_generated_reports"].append(dict(params))
            return FakeLimiraPostgresResult([row] if "returning" in sql else [])

        if "from limira_generated_reports" in sql and "report_id = :report_id" in sql:
            task = self.tasks.get(params["task_id"])
            row = self.generated_reports.get((params["task_id"], params["report_id"]))
            if not task or task["owner_user_id"] != params["owner_user_id"]:
                row = None
            return FakeLimiraPostgresResult([row] if row else [])

        if "from limira_generated_reports" in sql:
            rows = [
                row
                for (task_id, _report_id), row in self.generated_reports.items()
                if task_id == params["task_id"]
            ]
            return FakeLimiraPostgresResult(rows)

        for table in self.typed_inserts:
            if f"insert into {table}" in sql:
                self.typed_inserts[table].append(dict(params))
                return FakeLimiraPostgresResult([])

        return FakeLimiraPostgresResult([])


class FakeLimiraPostgresResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)
