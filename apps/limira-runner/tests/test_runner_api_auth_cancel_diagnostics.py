import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest

from runner_api import ACTIVE_TASKS_KEY, CANCELLED_TASKS_KEY
from test_runner_api import (
    ADMIN_HEADERS,
    ARCHIVE_MEMBERS,
    USER_A_HEADERS,
    USER_B_HEADERS,
    BackgroundExecutionProbe,
    CancellationProbe,
    CompleteFailWriter,
    RenewingCancelRaceStore,
    StartFailWriter,
    StreamClaimRaceStore,
    ZipFailWriter,
    assert_diagnostic_archive,
    assert_public_task_response_hides_internal_identifiers,
    assert_zip_members,
    cancelled_secret_stream,
    cancelled_stream,
    complete_task,
    failed_stream,
    make_client,
    parse_sse,
    seed_queued_task,
    seed_running_task,
    start_task,
    wait_for_task_status,
)


@pytest.mark.asyncio
async def test_runner_api_rejects_foreign_user_and_not_ready_download(tmp_path):
    probe = BackgroundExecutionProbe()
    client, _store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        foreign_status = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_B_HEADERS,
        )
        assert foreign_status.status == 404

        foreign_events = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_B_HEADERS,
        )
        assert foreign_events.status == 404

        not_ready_download = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert not_ready_download.status == 409
        assert (await not_ready_download.json())["error"] == "archive_not_ready"
        probe.release.set()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_allows_admin_but_blocks_foreign_download(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        await events_response.text()

        foreign_download = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=USER_B_HEADERS,
        )
        assert foreign_download.status == 404

        admin_status = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=ADMIN_HEADERS,
        )
        assert admin_status.status == 200

        admin_download = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=ADMIN_HEADERS,
        )
        assert admin_download.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_foreign_user_denied_for_status(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{start_payload['task_id']}",
            headers=USER_B_HEADERS,
        )
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_foreign_user_denied_for_events(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{start_payload['task_id']}/events",
            headers=USER_B_HEADERS,
        )
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_foreign_user_denied_for_archive_download(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        task_id = await complete_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=USER_B_HEADERS,
        )
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_foreign_user_denied_for_cancel(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.post(
            f"/limira-runner/tasks/{start_payload['task_id']}/cancel",
            headers=USER_B_HEADERS,
        )
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_status(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{start_payload['task_id']}",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] in {"queued", "running", "completed"}
        assert_public_task_response_hides_internal_identifiers(payload)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_events(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{start_payload['task_id']}/events",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        sse_events = parse_sse(await response.text())
        assert [event["type"] for event in sse_events] == ["heartbeat", "message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_archive_download(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        task_id = await complete_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            assert sorted(archive.namelist()) == ARCHIVE_MEMBERS
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_cancel(tmp_path):
    probe = CancellationProbe()
    client, store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        await asyncio.wait_for(probe.started.wait(), timeout=1)
        response = await client.post(
            f"/limira-runner/tasks/{start_payload['task_id']}/cancel",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] == "running"
        assert payload["archive_status"] == "pending"
        assert payload["cancel_requested"] is True
        assert_public_task_response_hides_internal_identifiers(payload)
        await wait_for_task_status(store, start_payload["task_id"], "cancelled")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_queued_cancel(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        record = seed_queued_task(store)
        response = await client.post(
            f"/limira-runner/tasks/{record.task_id}/cancel",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] == "cancelled"
        assert payload["archive_status"] == "ready"
        assert payload["cancel_requested"] is True
        assert_public_task_response_hides_internal_identifiers(payload)
        record = store.get_task(record.task_id)
        assert record.status == "cancelled"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_endpoint_stops_active_stream_and_archives(tmp_path):
    probe = CancellationProbe()
    client, store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)
        assert task_id in client.server.app[ACTIVE_TASKS_KEY]

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        assert (await cancel_response.json())["cancel_requested"] is True

        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        await asyncio.wait_for(events_response.text(), timeout=1)
        await asyncio.wait_for(probe.stopped.wait(), timeout=1)
        assert probe.check_count > 0

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        assert status_payload["status"] == "cancelled"
        assert status_payload["archive_status"] == "ready"
        assert_public_task_response_hides_internal_identifiers(status_payload)

        record = store.get_task(task_id)
        archive_dir = Path(record.archive_dir)
        metadata = json.loads((archive_dir / "metadata.json").read_text())
        assert metadata["status"] == "cancelled"
        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "Limira Research Cancelled" in report
        assert_zip_members(record.archive_zip_path)
        assert task_id not in client.server.app[ACTIVE_TASKS_KEY]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_finalizes_running_task_without_active_worker(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        seeded = seed_queued_task(store)
        task_id = seeded.task_id
        claimed = store.claim_queued_task(
            task_id,
            started_at="2026-06-06T12:30:00+00:00",
        )
        assert claimed is not None
        assert task_id not in client.server.app[ACTIVE_TASKS_KEY]

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()

        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "ready"
        assert cancel_payload["download_url"] == (
            f"/limira-runner/tasks/{task_id}/archive.zip"
        )
        assert_public_task_response_hides_internal_identifiers(cancel_payload)
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]
        assert task_id not in client.server.app[ACTIVE_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "cancelled"
        assert record.archive_status == "ready"
        assert record.completed_at is not None
        archive_dir = Path(record.archive_dir)
        metadata = json.loads((archive_dir / "metadata.json").read_text())
        assert metadata["status"] == "cancelled"
        assert metadata["error"] == (
            "task cancelled because no active stream worker was registered"
        )
        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "Limira Research Cancelled" in report
        assert_zip_members(record.archive_zip_path)

        retry_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert retry_response.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_rejects_running_task_owned_by_healthy_external_lease(
    tmp_path,
):
    client, store = await make_client(tmp_path)
    try:
        seeded = seed_running_task(
            store,
            worker_id="runner-other:task-external:worker",
            lease_expires_at="2026-06-06T13:00:00+00:00",
        )

        cancel_response = await client.post(
            f"/limira-runner/tasks/{seeded.task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        cancel_payload = await cancel_response.json()
        current = store.get_task(seeded.task_id)

        assert cancel_response.status == 409
        assert cancel_payload["error"] == "task_owned_by_active_worker"
        assert current.status == "running"
        assert current.worker_id == "runner-other:task-external:worker"
        assert current.lease_expires_at == "2026-06-06T13:00:00+00:00"
        assert current.completed_at is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_finalizes_expired_external_lease_without_worker(
    tmp_path,
):
    client, store = await make_client(tmp_path)
    try:
        seeded = seed_running_task(
            store,
            worker_id="runner-other:task-expired:worker",
            lease_expires_at="2026-06-06T11:00:00+00:00",
        )

        cancel_response = await client.post(
            f"/limira-runner/tasks/{seeded.task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        cancel_payload = await cancel_response.json()
        current = store.get_task(seeded.task_id)

        assert cancel_response.status == 200
        assert cancel_payload["status"] == "cancelled"
        assert current.status == "cancelled"
        assert current.error.startswith(
            "task cancelled because no active stream worker was registered"
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_preserves_lease_renewed_during_no_worker_cancel(
    tmp_path,
):
    store = RenewingCancelRaceStore(tmp_path / "tasks.sqlite3")
    client, store = await make_client(tmp_path, task_store=store)
    try:
        seeded = seed_running_task(
            store,
            worker_id="runner-other:task-cancel-race:worker",
            lease_expires_at="2026-06-06T11:00:00+00:00",
        )

        cancel_response = await client.post(
            f"/limira-runner/tasks/{seeded.task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        cancel_payload = await cancel_response.json()
        current = store.get_task(seeded.task_id)

        assert cancel_response.status == 409
        assert cancel_payload["error"] == "task_owned_by_active_worker"
        assert store.renewed_during_cancel is True
        assert current.status == "running"
        assert current.worker_id == "runner-other:task-cancel-race:worker"
        assert current.lease_expires_at == "2026-06-06T13:00:00+00:00"
        assert current.completed_at is None
        assert current.error is None
        assert current.archive_dir is None
        assert store.list_task_events(seeded.task_id) == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_endpoint_finalizes_queued_task(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        task_id = seed_queued_task(store).task_id

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()
        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "ready"
        assert_public_task_response_hides_internal_identifiers(cancel_payload)
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        archive_dir = Path(record.archive_dir)
        metadata = json.loads((archive_dir / "metadata.json").read_text())
        assert metadata["status"] == "cancelled"
        assert metadata["error"] == "task cancelled before stream started"
        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "Limira Research Cancelled" in report
        assert_zip_members(record.archive_zip_path)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_queued_cancel_start_failure_sets_archive_failed(tmp_path):
    client, store = await make_client(tmp_path, writer_cls=StartFailWriter)
    try:
        task_id = seed_queued_task(store).task_id

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()
        serialized_payload = json.dumps(cancel_payload)

        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "failed"
        assert cancel_payload["download_url"] is None
        assert cancel_payload["error"] == "task cancelled before stream started"
        assert cancel_payload["warnings"] == [
            "queued cancellation archive finalization failed: Authorization: [REDACTED]"
        ]
        assert "setupsecret123456" not in serialized_payload
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "cancelled"
        assert record.archive_status == "failed"
        assert record.archive_dir is None
        assert record.archive_zip_path is None
        assert "setupsecret123456" not in json.dumps(record.to_dict())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_queued_cancel_complete_failure_sets_archive_failed(tmp_path):
    client, store = await make_client(tmp_path, writer_cls=CompleteFailWriter)
    try:
        task_id = seed_queued_task(store).task_id

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()
        serialized_payload = json.dumps(cancel_payload)

        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "failed"
        assert cancel_payload["download_url"] is None
        assert cancel_payload["error"] == "task cancelled before stream started"
        assert cancel_payload["warnings"] == [
            "queued cancellation archive finalization failed: Authorization: [REDACTED]"
        ]
        assert "finalsecret123456" not in serialized_payload
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "cancelled"
        assert record.archive_status == "failed"
        assert record.archive_dir is not None
        assert record.archive_zip_path is None
        assert "finalsecret123456" not in json.dumps(record.to_dict())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_queued_cancel_keeps_signal_when_stream_claim_wins(tmp_path):
    store = StreamClaimRaceStore(tmp_path / "tasks.sqlite3")
    client, _store = await make_client(tmp_path, task_store=store)
    try:
        task_id = seed_queued_task(store).task_id

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()

        assert store.claimed_during_cancel is True
        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "running"
        assert cancel_payload["archive_status"] == "pending"
        assert_public_task_response_hides_internal_identifiers(cancel_payload)
        assert task_id in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "running"
        assert record.archive_status == "pending"
        assert record.archive_dir is None
        assert record.started_at == "2026-06-06T12:59:59+00:00"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_allows_duplicate_running_event_stream_subscription(tmp_path):
    probe = CancellationProbe()
    client, _store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        duplicate_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert duplicate_response.status == 200
        assert probe.stream_count == 1

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        await asyncio.wait_for(events_response.text(), timeout=1)
        await asyncio.wait_for(duplicate_response.text(), timeout=1)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_transport_close_does_not_cancel_background_task(tmp_path):
    probe = CancellationProbe()
    transport_closed = False

    def transport_closing(_request):
        return transport_closed

    client, _store = await make_client(
        tmp_path,
        stream_events=probe.stream,
        transport_closing=transport_closing,
    )
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        first_line = await asyncio.wait_for(
            events_response.content.readline(),
            timeout=1,
        )
        assert first_line.startswith(b"data: ")
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        transport_closed = True
        await asyncio.sleep(0.05)
        assert not probe.stopped.is_set()

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        await asyncio.wait_for(probe.stopped.wait(), timeout=1)
        await asyncio.wait_for(events_response.text(), timeout=1)

        status_payload = None
        for _ in range(20):
            status_response = await client.get(
                f"/limira-runner/tasks/{task_id}",
                headers=USER_A_HEADERS,
            )
            status_payload = await status_response.json()
            if status_payload["status"] == "cancelled":
                break
            await asyncio.sleep(0.02)
        assert status_payload["status"] == "cancelled"
        assert status_payload["archive_status"] == "ready"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_endpoint_enforces_owner_and_admin(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        foreign_task = seed_queued_task(store)
        foreign_cancel = await client.post(
            f"/limira-runner/tasks/{foreign_task.task_id}/cancel",
            headers=USER_B_HEADERS,
        )
        assert foreign_cancel.status == 404

        admin_task = seed_queued_task(store)
        admin_cancel = await client.post(
            f"/limira-runner/tasks/{admin_task.task_id}/cancel",
            headers=ADMIN_HEADERS,
        )
        assert admin_cancel.status == 200
        admin_payload = await admin_cancel.json()
        assert admin_payload["status"] == "cancelled"
        assert admin_payload["archive_status"] == "ready"
        assert admin_payload["cancel_requested"] is True
        assert_public_task_response_hides_internal_identifiers(admin_payload)

        record = store.get_task(admin_task.task_id)
        assert record.status == "cancelled"
        assert record.archive_zip_path is not None

        completed_events = await client.get(
            f"/limira-runner/tasks/{admin_task.task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert completed_events.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_validation_rejects_untrusted_inputs(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        missing_auth = await client.post(
            "/limira-runner/research",
            json={"query": "x"},
        )
        assert missing_auth.status == 401

        body_user = await client.post(
            "/limira-runner/research",
            headers=USER_A_HEADERS,
            json={"query": "x", "user_id": "attacker"},
        )
        assert body_user.status == 400

        empty_query = await client.post(
            "/limira-runner/research",
            headers=USER_A_HEADERS,
            json={"query": "  "},
        )
        assert empty_query.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_failed_outcome_writes_scrubbed_diagnostic_archive(tmp_path):
    client, store = await make_client(tmp_path, stream_events=failed_stream)
    try:
        task = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        assert payload["status"] == "failed"
        assert payload["archive_status"] == "ready"
        assert "dXNlcjpzZWNyZXQ" not in json.dumps(payload)

        assert_diagnostic_archive(
            store,
            task["task_id"],
            expected_status="failed",
            forbidden_text="dXNlcjpzZWNyZXQ",
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancelled_outcome_writes_scrubbed_diagnostic_archive(
    tmp_path,
):
    client, store = await make_client(tmp_path, stream_events=cancelled_secret_stream)
    try:
        task = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        assert payload["status"] == "cancelled"
        assert payload["archive_status"] == "ready"
        assert "cancelledsecret123456" not in json.dumps(payload)

        assert_diagnostic_archive(
            store,
            task["task_id"],
            expected_status="cancelled",
            forbidden_text="cancelledsecret123456",
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_archive_failed_exposes_warning_without_failing_research(
    tmp_path,
):
    client, store = await make_client(tmp_path, writer_cls=ZipFailWriter)
    try:
        task = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        assert payload["status"] == "completed"
        assert payload["archive_status"] == "failed"
        assert payload["download_url"] is None
        assert payload["warnings"] == ["archive.zip creation failed: zip unavailable"]

        record = store.get_task(task["task_id"])
        assert record.status == "completed"
        assert record.archive_status == "failed"
        assert record.archive_zip_path is None

        download_response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert download_response.status == 409
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_failed_cancelled_and_archive_failed_outcomes(tmp_path):
    failed_client, _store = await make_client(
        tmp_path / "failed", stream_events=failed_stream
    )
    try:
        failed_task = await start_task(failed_client)
        failed_events = await failed_client.get(
            f"/limira-runner/tasks/{failed_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert failed_events.status == 200
        failed_status = await failed_client.get(
            f"/limira-runner/tasks/{failed_task['task_id']}",
            headers=USER_A_HEADERS,
        )
        failed_payload = await failed_status.json()
        assert failed_payload["status"] == "failed"
        assert failed_payload["archive_status"] == "ready"
        assert "dXNlcjpzZWNyZXQ" not in json.dumps(failed_payload)
    finally:
        await failed_client.close()

    cancelled_client, _store = await make_client(
        tmp_path / "cancelled",
        stream_events=cancelled_stream,
    )
    try:
        cancelled_task = await start_task(cancelled_client)
        cancelled_events = await cancelled_client.get(
            f"/limira-runner/tasks/{cancelled_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert cancelled_events.status == 200
        cancelled_status = await cancelled_client.get(
            f"/limira-runner/tasks/{cancelled_task['task_id']}",
            headers=USER_A_HEADERS,
        )
        cancelled_payload = await cancelled_status.json()
        assert cancelled_payload["status"] == "cancelled"
        assert cancelled_payload["archive_status"] == "ready"
    finally:
        await cancelled_client.close()

    archive_failed_client, _store = await make_client(
        tmp_path / "archive-failed",
        writer_cls=ZipFailWriter,
    )
    try:
        archive_failed_task = await start_task(archive_failed_client)
        archive_failed_events = await archive_failed_client.get(
            f"/limira-runner/tasks/{archive_failed_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert archive_failed_events.status == 200
        archive_failed_status = await archive_failed_client.get(
            f"/limira-runner/tasks/{archive_failed_task['task_id']}",
            headers=USER_A_HEADERS,
        )
        archive_failed_payload = await archive_failed_status.json()
        assert archive_failed_payload["status"] == "completed"
        assert archive_failed_payload["archive_status"] == "failed"

        archive_failed_download = await archive_failed_client.get(
            f"/limira-runner/tasks/{archive_failed_task['task_id']}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert archive_failed_download.status == 409
    finally:
        await archive_failed_client.close()
