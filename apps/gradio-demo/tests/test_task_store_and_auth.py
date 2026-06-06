import sqlite3

import pytest

from auth_adapter import AuthError, authenticate_headers, reject_body_user_id
from task_store import TaskStore


def test_task_store_creates_updates_and_lists_by_user(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
        model_summary={"provider": "deepseek"},
    )
    store.create_task(
        task_id="task-b",
        user_id="user-a",
        query="second",
        created_at="2026-06-06T12:01:00+00:00",
    )
    store.create_task(
        task_id="task-c",
        user_id="user-b",
        query="third",
        created_at="2026-06-06T12:02:00+00:00",
    )

    updated = store.update_task(
        "task-a",
        status="completed",
        archive_status="ready",
        archive_dir="/archives/task-a",
        archive_zip_path="/archives/task-a/archive.zip",
        completed_at="2026-06-06T12:10:00+00:00",
        warnings=["warning"],
    )

    assert updated.status == "completed"
    assert updated.archive_status == "ready"
    assert updated.warnings == ["warning"]
    assert store.user_owns_task("task-a", "user-a") is True
    assert store.user_owns_task("task-a", "user-b") is False
    assert [record.task_id for record in store.list_user_tasks("user-a")] == [
        "task-b",
        "task-a",
    ]


def test_task_store_enforces_unique_task_id(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(
            task_id="task-a",
            user_id="user-b",
            query="duplicate",
            created_at="2026-06-06T12:01:00+00:00",
        )


def test_task_store_rejects_invalid_status_updates(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
    )

    with pytest.raises(ValueError):
        store.update_task("task-a", status="done")

    with pytest.raises(ValueError):
        store.update_task("task-a", archive_status="missing")


def test_auth_adapter_accepts_trusted_headers_only():
    auth = authenticate_headers(
        {
            "X-MiroThinker-Service-Token": "shared",
            "X-OpenWebUI-User-Id": "user-a",
            "X-OpenWebUI-User-Role": "admin",
        },
        service_token="shared",
    )

    assert auth.user_id == "user-a"
    assert auth.is_admin is True


def test_auth_adapter_rejects_invalid_or_body_user_id():
    with pytest.raises(AuthError) as invalid_token:
        authenticate_headers(
            {
                "X-MiroThinker-Service-Token": "wrong",
                "X-OpenWebUI-User-Id": "user-a",
            },
            service_token="shared",
        )
    assert invalid_token.value.code == "invalid_service_token"

    with pytest.raises(AuthError) as missing_user:
        authenticate_headers(
            {"X-MiroThinker-Service-Token": "shared"},
            service_token="shared",
        )
    assert missing_user.value.code == "missing_user_id"

    with pytest.raises(AuthError) as body_user:
        reject_body_user_id({"query": "x", "user_id": "attacker"})
    assert body_user.value.status == 400
