from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

try:
    from open_webui.utils.auth import (
        get_admin_user as _open_webui_admin_user,
        get_verified_user as _open_webui_verified_user,
    )
except Exception:  # pragma: no cover - exercised only when imported without deps
    _open_webui_admin_user = None
    _open_webui_verified_user = None


ARCHIVE_MEMBERS = {"trace.json", "report.md", "metadata.json", "report.html"}
FORBIDDEN_BROWSER_SUBSTRINGS = {
    "/mirothinker/",
    "limra-runner:8091",
    "localhost:8091",
    "RUNNER_SERVICE_TOKEN",
}

router = APIRouter()


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    scenario: str | None = None


@dataclass(frozen=True)
class LimraUser:
    id: str
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class LimraTask:
    task_id: str
    owner_user_id: str
    query: str
    status: str = "queued"
    archive_status: str = "pending"
    runner_task_id: str | None = None
    scenario: str | None = None
    error: str | None = None
    model_summary: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "query": self.query,
            "status": self.status,
            "archive_status": self.archive_status,
            "scenario": self.scenario,
            "error": self.error,
            "model_summary": self.model_summary or {},
            "download_url": f"/api/limra/tasks/{self.task_id}/archive.zip"
            if self.archive_status == "ready"
            else None,
            "events_url": f"/api/limra/tasks/{self.task_id}/events",
            "artifacts_url": f"/api/limra/tasks/{self.task_id}/artifacts",
        }


class LimraTaskRepository(Protocol):
    def create_task(
        self,
        *,
        task_id: str,
        owner_user_id: str,
        query: str,
        scenario: str | None,
        runner_task_id: str | None,
    ) -> LimraTask: ...

    def get_task(self, task_id: str) -> LimraTask | None: ...

    def get_user_task(self, task_id: str, owner_user_id: str) -> LimraTask | None: ...


class InMemoryLimraTaskRepository:
    def __init__(self) -> None:
        self.tasks: dict[str, LimraTask] = {}

    def create_task(
        self,
        *,
        task_id: str,
        owner_user_id: str,
        query: str,
        scenario: str | None,
        runner_task_id: str | None,
    ) -> LimraTask:
        task = LimraTask(
            task_id=task_id,
            owner_user_id=owner_user_id,
            query=query,
            scenario=scenario,
            runner_task_id=runner_task_id,
        )
        self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> LimraTask | None:
        return self.tasks.get(task_id)

    def get_user_task(self, task_id: str, owner_user_id: str) -> LimraTask | None:
        task = self.get_task(task_id)
        if not task or task.owner_user_id != owner_user_id:
            return None
        return task


class RunnerArchiveClient:
    def __init__(
        self,
        *,
        runner_url: str | None = None,
        service_token: str | None = None,
    ) -> None:
        self.runner_url = (runner_url or os.getenv("LIMRA_RUNNER_INTERNAL_URL") or "").rstrip(
            "/"
        )
        self.service_token = service_token or os.getenv("LIMRA_RUNNER_SERVICE_TOKEN")

    async def download_archive(self, task: LimraTask, user: LimraUser) -> bytes:
        if not self.runner_url:
            raise HTTPException(status_code=503, detail="runner_url_not_configured")
        runner_task_id = task.runner_task_id or task.task_id
        url = f"{self.runner_url}/mirothinker/tasks/{runner_task_id}/archive.zip"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=runner_service_headers(user, self.service_token))
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="archive_not_found")
        if response.status_code == 409:
            raise HTTPException(status_code=409, detail="archive_not_ready")
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="runner_archive_proxy_failed")
        return response.content


def runner_service_headers(user: LimraUser, service_token: str | None) -> dict[str, str]:
    headers = {
        "X-OpenWebUI-User-Id": user.id,
        "X-OpenWebUI-User-Role": user.role,
    }
    if service_token:
        headers["X-MiroThinker-Service-Token"] = service_token
    return headers


def _open_webui_verified_dependency():
    if _open_webui_verified_user is None:
        async def _missing_verified_user():
            raise HTTPException(status_code=401, detail="open_webui_auth_unavailable")

        return _missing_verified_user
    return _open_webui_verified_user


def _open_webui_admin_dependency():
    if _open_webui_admin_user is None:
        async def _missing_admin_user():
            raise HTTPException(status_code=401, detail="open_webui_auth_unavailable")

        return _missing_admin_user
    return _open_webui_admin_user


async def get_current_limra_user(user=Depends(_open_webui_verified_dependency())) -> LimraUser:
    return _limra_user_from_open_webui_user(user)


async def get_current_limra_admin(user=Depends(_open_webui_admin_dependency())) -> LimraUser:
    return _limra_user_from_open_webui_user(user)


def get_task_repository(request: Request) -> LimraTaskRepository:
    repo = getattr(request.app.state, "limra_task_repository", None)
    if repo is None:
        repo = InMemoryLimraTaskRepository()
        request.app.state.limra_task_repository = repo
    return repo


def get_archive_client(request: Request) -> RunnerArchiveClient:
    client = getattr(request.app.state, "limra_archive_client", None)
    if client is None:
        client = RunnerArchiveClient()
        request.app.state.limra_archive_client = client
    return client


@router.post("/research", status_code=202)
async def create_research_task(
    form_data: dict[str, Any],
    request: Request,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    if "user_id" in form_data or "owner_user_id" in form_data:
        raise HTTPException(status_code=400, detail="user_id_not_allowed")
    request_data = ResearchRequest.model_validate(form_data)
    task_id = str(uuid.uuid4())
    task = repo.create_task(
        task_id=task_id,
        owner_user_id=user.id,
        query=request_data.query.strip(),
        scenario=request_data.scenario,
        runner_task_id=None,
    )
    return _assert_browser_safe(
        {
            "task_id": task.task_id,
            "status": task.status,
            "task_url": f"/api/limra/tasks/{task.task_id}",
            "events_url": f"/api/limra/tasks/{task.task_id}/events",
            "artifacts_url": f"/api/limra/tasks/{task.task_id}/artifacts",
        }
    )


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    task = _get_owned_task(repo, task_id, user)
    return _assert_browser_safe(task.public_dict())


@router.get("/tasks/{task_id}/events")
async def get_task_events(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> StreamingResponse:
    _get_owned_task(repo, task_id, user)
    return StreamingResponse(_empty_event_stream(), media_type="text/event-stream")


@router.get("/tasks/{task_id}/artifacts")
async def get_task_artifacts(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, list[Any]]:
    _get_owned_task(repo, task_id, user)
    return {
        "evidence": [],
        "entities": [],
        "relations": [],
        "timeline": [],
        "map_features": [],
        "report_sections": [],
    }


@router.get("/tasks/{task_id}/archive.zip")
async def download_task_archive(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    archive_client: RunnerArchiveClient = Depends(get_archive_client),
) -> Response:
    task = _get_owned_task(repo, task_id, user)
    return await _download_archive(task, user, archive_client)


@router.get("/admin/tasks/{task_id}")
async def admin_get_task(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_admin),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    payload = task.public_dict()
    payload["owner_user_id"] = task.owner_user_id
    payload["admin"] = user.id
    return _assert_browser_safe(payload)


@router.get("/admin/tasks/{task_id}/archive.zip")
async def admin_download_task_archive(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_admin),
    repo: LimraTaskRepository = Depends(get_task_repository),
    archive_client: RunnerArchiveClient = Depends(get_archive_client),
) -> Response:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return await _download_archive(task, user, archive_client)


@router.post("/uploads", status_code=501)
async def upload_document(
    file: UploadFile,
    user: LimraUser = Depends(get_current_limra_user),
) -> dict[str, str]:
    return {"error": "upload_not_implemented", "user_id": user.id, "filename": file.filename or ""}


@router.post("/tasks/{task_id}/reports/pdf", status_code=501)
async def export_task_pdf(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, str]:
    _get_owned_task(repo, task_id, user)
    return {"error": "pdf_export_not_implemented"}


async def _download_archive(
    task: LimraTask,
    user: LimraUser,
    archive_client: RunnerArchiveClient,
) -> Response:
    if task.archive_status != "ready":
        raise HTTPException(status_code=409, detail="archive_not_ready")
    archive_bytes = await archive_client.download_archive(task, user)
    validate_archive_zip(archive_bytes)
    return Response(
        archive_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="archive.zip"'},
    )


def validate_archive_zip(archive_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=502, detail="invalid_archive_zip") from exc
    if names != ARCHIVE_MEMBERS:
        raise HTTPException(status_code=502, detail="invalid_archive_members")
    if any(name.startswith("/") or ".." in name.split("/") for name in names):
        raise HTTPException(status_code=502, detail="unsafe_archive_member")


def _get_owned_task(
    repo: LimraTaskRepository,
    task_id: str,
    user: LimraUser,
) -> LimraTask:
    task = repo.get_user_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return task


async def _empty_event_stream() -> AsyncIterator[bytes]:
    if False:
        yield b""


def _assert_browser_safe(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False)
    leaked = [needle for needle in FORBIDDEN_BROWSER_SUBSTRINGS if needle in encoded]
    if leaked:
        raise HTTPException(status_code=500, detail="browser_payload_leak")
    return payload


def _limra_user_from_open_webui_user(user: Any) -> LimraUser:
    user_id = getattr(user, "id", None) or getattr(user, "email", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="missing_user_id")
    return LimraUser(id=str(user_id), role=str(getattr(user, "role", "user")))
