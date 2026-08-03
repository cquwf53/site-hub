from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import pytest

from site_hub.jobs.service import JobService
from site_hub.models import JobKind
from site_hub.services.data_store import DataLayerStore
from site_hub.services.events import EventBroker
from site_hub.services.memory_store import MemoryStore
from site_hub.workers.supervisor import NodeWorkerSupervisor


FAKE_WORKER = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def send(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()

send({"jsonrpc":"2.0","id":"hello","method":"worker.hello","params":{"protocol":1,"adapters":[]}})
json.loads(sys.stdin.readline())
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
Path(os.environ["SITEHUB_TEST_CHILD_PID_FILE"]).write_text(str(child.pid), encoding="ascii")
send({"jsonrpc":"2.0","id":"ready","method":"worker.ready","params":{}})
for line in sys.stdin:
    message = json.loads(line)
    if "method" not in message:
        continue
    if message["method"] == "worker.shutdown":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"accepted":True}})
        break
    if message["method"] == "worker.ping":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"ok":True}})
'''


async def _wait_until(predicate, *, timeout: float = 12.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not reached before timeout")


async def _wait_for_job(store, job_id: UUID, status: str, *, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = await store.get_job(job_id)
        if row and row["status"] == status:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {status}")


class RecordingPeer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.started = asyncio.Event()

    async def request(self, method: str, params: dict, *, timeout: float):
        self.calls.append((method, params))
        if method == "job.start":
            self.started.set()
        return {"accepted": True}


class RecordingEvents:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))


def _bare_supervisor(jobs: JobService, tmp_path: Path, peer: RecordingPeer):
    profiles = tmp_path / "profiles"; profiles.mkdir(exist_ok=True)
    artifacts = tmp_path / "artifacts"; artifacts.mkdir(exist_ok=True)
    supervisor = NodeWorkerSupervisor(
        jobs, executable="node", entry=tmp_path / "worker.js",
        profiles_dir=profiles, artifacts_dir=artifacts,
    )
    supervisor.instance_id = "test-worker"
    supervisor._peer = peer
    supervisor._ready.set()
    return supervisor


@pytest.mark.asyncio
async def test_profile_verification_uses_dedicated_read_only_worker_rpc(tmp_path):
    store = MemoryStore()
    site = await store.create_site({
        "name": "Site", "url": "https://example.test/account", "status": "active",
    })
    account = await store.create_account(UUID(str(site["id"])), {
        "name": "Primary", "adapter": "worker-adapter", "enabled": False,
    })
    peer = RecordingPeer()
    supervisor = _bare_supervisor(JobService(store, EventBroker()), tmp_path, peer)

    await supervisor.verify_session(UUID(str(account["id"])))

    method, params = peer.calls[-1]
    assert method == "session.verify"
    assert params["accountId"] == str(account["id"])
    assert params["site"]["url"] == "https://example.test/account"
    assert Path(params["profileDir"]).resolve().parent == (tmp_path / "profiles").resolve()


@pytest.mark.asyncio
async def test_session_event_is_resanitized_before_persist_and_publish(tmp_path):
    store = MemoryStore()
    site = await store.create_site({
        "name": "Site", "url": "https://example.test/account", "status": "active",
    })
    account = await store.create_account(UUID(str(site["id"])), {
        "name": "Primary", "adapter": "worker-adapter", "enabled": False,
    })
    events = RecordingEvents()
    supervisor = _bare_supervisor(JobService(store, events), tmp_path, RecordingPeer())

    result = await supervisor._on_session_event({
        "accountId": str(account["id"]),
        "action": "login_complete",
        "state": "error",
        "currentUrl": "https://user:pass@example.test/account?token=url-secret#fragment",
        "error": "token=message-secret at https://example.test/?token=url-secret",
    })

    assert result == {"accepted": True}
    persisted = await store.get_account(UUID(str(account["id"])))
    assert persisted and persisted["profile"]["state"] == "error"
    assert "message-secret" not in str(persisted["profile"]["verification_error"])
    topic, event = events.published[-1]
    assert topic == "session"
    assert event["currentUrl"] == "https://example.test/account"
    assert "?" not in event["currentUrl"] and "#" not in event["currentUrl"]
    assert "message-secret" not in str(event["error"])
    assert "url-secret" not in str(event)


@pytest.mark.asyncio
async def test_dispatch_gate_skips_job_when_checkin_schedule_is_disabled(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    loop_task = None
    try:
        site = await store.create_site({
            "name": "Site", "url": "https://example.test", "status": "active",
        })
        account = await store.create_account(UUID(str(site["id"])), {
            "name": "Primary", "adapter": "api", "enabled": True,
        })
        jobs = JobService(store, EventBroker())
        task, _ = await jobs.enqueue_checkin(UUID(str(account["id"])))
        with store.database.transaction(immediate=True) as conn:
            conn.execute(
                """UPDATE schedules SET enabled=0 WHERE capability_instance_id IN
                   (SELECT id FROM capability_instances WHERE account_id=?)""",
                (str(account["id"]),),
            )
        assert (await store.get_account(UUID(str(account["id"]))))["enabled"] is False

        peer = RecordingPeer()
        supervisor = _bare_supervisor(jobs, tmp_path, peer)
        loop_task = asyncio.create_task(supervisor._dispatch_loop())
        skipped = await _wait_for_job(store, task.job_id, "skipped")
        assert skipped["result"]["reason_code"] == "disabled"
        assert [method for method, _ in peer.calls if method == "job.start"] == []
        supervisor._ready.clear()
    finally:
        if loop_task:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        await store.close()


@pytest.mark.asyncio
async def test_retried_unknown_checkin_is_marked_for_reconcile_and_stale_cancel_is_acked(tmp_path):
    store = MemoryStore()
    site = await store.create_site({
        "name": "Site", "url": "https://example.test", "status": "active",
    })
    account = await store.create_account(UUID(str(site["id"])), {
        "name": "Primary", "adapter": "api", "enabled": True,
    })
    jobs = JobService(store, EventBroker())
    task, _ = await jobs.enqueue_checkin(UUID(str(account["id"])))
    first = await jobs.lease(JobKind.CHECKIN, "lost-worker")
    assert first
    await jobs.mark_running(task.job_id, str(first["lease_token"]))
    assert (await jobs.cancel(task.job_id))["status"] == "effect_unknown"
    await jobs.retry(task.job_id)

    peer = RecordingPeer()
    supervisor = _bare_supervisor(jobs, tmp_path, peer)
    loop_task = asyncio.create_task(supervisor._dispatch_loop())
    try:
        await asyncio.wait_for(peer.started.wait(), timeout=2)
        start_params = next(params for method, params in peer.calls if method == "job.start")
        assert start_params["reconcile"] is True
        running = await store.get_job(task.job_id)
        assert running and running["status"] == "running" and running["attempt"] == 2

        assert (await jobs.cancel(task.job_id))["status"] == "effect_unknown"
        await supervisor.cancel_job(task.job_id)
        assert any(method == "job.cancel" for method, _ in peer.calls)
        stale_ack = await supervisor._on_job_complete({
            "jobId": str(task.job_id), "leaseToken": start_params["leaseToken"],
            "accountId": str(account["id"]), "status": "effect_unknown",
            "reasonCode": "cancel_requested", "message": "cancelled", "artifacts": [],
        })
        assert stale_ack == {
            "accepted": False, "stale": True, "cancelRequested": True,
        }
    finally:
        supervisor._ready.clear()
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_effect_unknown_timeout_persists_reason_in_job_and_run(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    loop_task = None
    try:
        site = await store.create_site({
            "name": "Site", "url": "https://example.test", "status": "active",
        })
        account = await store.create_account(UUID(str(site["id"])), {
            "name": "Primary", "adapter": "api", "enabled": True,
        })
        jobs = JobService(store, EventBroker())
        task, _ = await jobs.enqueue_checkin(UUID(str(account["id"])))
        peer = RecordingPeer()
        supervisor = _bare_supervisor(jobs, tmp_path, peer)
        loop_task = asyncio.create_task(supervisor._dispatch_loop())
        await asyncio.wait_for(peer.started.wait(), timeout=2)
        start_params = next(params for method, params in peer.calls if method == "job.start")

        assert await supervisor._on_job_complete({
            "jobId": str(task.job_id), "leaseToken": start_params["leaseToken"],
            "accountId": str(account["id"]), "status": "effect_unknown",
            "reasonCode": "timeout", "message": "Job deadline exceeded", "artifacts": [],
        }) == {"accepted": True}
        uncertain = await store.get_job(task.job_id)
        assert uncertain and uncertain["status"] == "effect_unknown"
        assert uncertain["error_kind"] == "timeout"
        assert uncertain["result"]["reason_code"] == "timeout"
        runs, total = await store.list_runs(page=1, page_size=20)
        assert total == 1
        assert runs[0]["status"] == "effect_unknown"
        assert runs[0]["reason_code"] == "timeout"
        supervisor._ready.clear()
    finally:
        if loop_task:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        await store.close()


def _process_is_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
@pytest.mark.asyncio
async def test_external_worker_kill_cleans_owned_child_and_restarts(tmp_path, monkeypatch):
    entry = tmp_path / "worker-node" / "src" / "fake_worker.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(FAKE_WORKER, encoding="utf-8")
    profiles = tmp_path / "profiles"; profiles.mkdir()
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    child_pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("SITEHUB_TEST_CHILD_PID_FILE", str(child_pid_file))

    supervisor = NodeWorkerSupervisor(
        JobService(MemoryStore(), EventBroker()), executable=sys.executable, entry=entry,
        profiles_dir=profiles, artifacts_dir=artifacts, handshake_seconds=5,
        ping_seconds=60, shutdown_seconds=2,
    )
    supervisor.start()
    try:
        await _wait_until(lambda: supervisor.state == "ready" and child_pid_file.is_file())
        first_root_pid = supervisor.pid
        first_child_pid = int(child_pid_file.read_text(encoding="ascii"))
        assert first_root_pid and _process_is_alive(first_child_pid)

        process = supervisor._process
        assert process is not None
        process.kill()  # Simulates an external hard kill; JS cleanup cannot run.

        await _wait_until(
            lambda: supervisor.restart_count >= 1
            and supervisor.state == "ready"
            and supervisor.pid not in {None, first_root_pid},
        )
        await _wait_until(lambda: not _process_is_alive(first_child_pid))
    finally:
        await supervisor.stop()
