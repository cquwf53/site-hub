#!/usr/bin/env python
"""Run real SiteHub/UI acceptance on an isolated loopback port and temporary DB.

Example:
    .venv/Scripts/python.exe tools/smoke-isolated.py \
        --frontend-dir .pytest-runs/optimization-dist

Never uses port 8686 or the project's data directory. Keeps logs, a screenshot,
and report.json under the printed system temporary directory for inspection.
Only terminates processes spawned by this run, verified by PID and creation time.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import psutil


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def isolated_data(value: str | Path) -> Path:
    target = Path(value).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    require(target.is_relative_to(temporary), "Smoke data must stay in the system temporary directory")
    require(target.name == "data" and target.parent.name.startswith("sitehub-isolated-smoke-"),
            "Refusing a data directory not created for isolated smoke")
    require(target != (ROOT / "data").resolve(), "Refusing production data")
    return target


def free_port(requested: int = 0) -> int:
    require(0 <= requested <= 65535 and requested != 8686, "Port 8686 is reserved for the existing service")
    with socket.socket() as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", requested))
        port = listener.getsockname()[1]
    return free_port() if port == 8686 else port


def identity(process: psutil.Process) -> tuple[int, float]:
    return process.pid, process.create_time()


def matching(key: tuple[int, float]) -> psutil.Process | None:
    try:
        process = psutil.Process(key[0])
        if process.create_time() == key[1] and process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
            return process
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


class OwnedProcess:
    """Own a spawned process tree; never look up or kill processes by image name."""

    def __init__(self, command: list[str], *, env: dict[str, str], log: Path):
        self.log = log
        with log.open("wb") as stream:
            self.child = subprocess.Popen(
                command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        self.key = identity(psutil.Process(self.child.pid))
        self.seen: set[tuple[int, float]] = {self.key}
        self.job = None
        if os.name == "nt":
            from site_hub.workers.supervisor import _WindowsKillOnCloseJob
            self.job = _WindowsKillOnCloseJob()
            require(matching(self.key) is not None, "Spawned process exited before ownership registration")
            try:
                self.job.assign(self.key[0])
            except BaseException:
                self.job.close()
                self.job = None
                process = matching(self.key)
                if process:
                    process.kill()
                raise

    def capture(self) -> None:
        root = matching(self.key)
        if root:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                for child in root.children(recursive=True):
                    key = identity(child)
                    if key[1] >= self.key[1]:
                        self.seen.add(key)

    def wait(self, timeout: float) -> int:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.capture()
            result = self.child.poll()
            if result is not None:
                return result
            time.sleep(0.05)
        raise TimeoutError(f"Owned process {self.key[0]} did not exit; inspect {self.log}")

    def own_descendant(self, pid: int) -> tuple[int, float]:
        self.capture()
        root = matching(self.key)
        require(root is not None, "Isolated server is no longer the spawned process")
        for child in root.children(recursive=True):
            if child.pid == pid:
                key = identity(child)
                require(key[1] >= self.key[1], "Child creation time predates the isolated root")
                self.seen.add(key)
                return key
        raise RuntimeError(f"Refusing to stop PID {pid}: it is not a child of this isolated server")

    def alive(self) -> list[tuple[int, float]]:
        self.capture()
        return [key for key in self.seen if matching(key) is not None]

    def job_active_count(self) -> int:
        if not self.job or self.job._handle is None:
            return len(self.alive())

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("total_user", ctypes.c_int64), ("total_kernel", ctypes.c_int64),
                ("period_user", ctypes.c_int64), ("period_kernel", ctypes.c_int64),
                ("faults", ctypes.c_uint32), ("total", ctypes.c_uint32),
                ("active", ctypes.c_uint32), ("terminated", ctypes.c_uint32),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel.QueryInformationJobObject.restype = ctypes.c_int
        accounting = Accounting()
        if not kernel.QueryInformationJobObject(
            ctypes.c_void_p(self.job._handle), 1, ctypes.byref(accounting),
            ctypes.sizeof(accounting), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active

    def cleanup(self) -> None:
        self.capture()
        if self.job:
            # The job was assigned only to our creation-time-verified root.
            # Its kill-on-close covers even descendants that exited/reparented
            # between psutil snapshots, without touching another application.
            self.job.close()
            self.job = None
        else:
            for key in reversed(sorted(self.seen, key=lambda item: item[1])):
                process = matching(key)
                if process:
                    with contextlib.suppress(psutil.NoSuchProcess):
                        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.child.wait(timeout=5)
        end = time.monotonic() + 5
        while self.alive() and time.monotonic() < end:
            time.sleep(0.05)


async def serve(arguments: argparse.Namespace) -> int:
    import uvicorn
    from site_hub.config import Settings
    from site_hub.main import create_app

    data = isolated_data(arguments.data_dir)
    require(arguments.port not in {0, 8686}, "Internal server requires a dedicated non-production port")
    nonce = os.environ.get("SITEHUB_SMOKE_NONCE", "")
    require(bool(nonce), "Internal --serve mode requires a smoke nonce")
    control = data.parent / "shutdown.request"
    settings = Settings(
        host="127.0.0.1", port=arguments.port, data_dir=data,
        frontend_dir=Path(arguments.frontend_dir).resolve(),
        node_executable=arguments.node,
        node_entry=ROOT / "worker-node" / "src" / "main.js",
        worker_ping_seconds=3, worker_shutdown_seconds=5,
    )
    app = create_app(settings=settings, start_background=True)

    @app.get("/__isolated_smoke__/identity", include_in_schema=False)
    async def smoke_identity():
        return {"nonce": nonce, "pid": os.getpid(), "data_dir": str(data)}

    # create_app already installed the SPA catch-all; the test-only identity
    # route must precede it or the probe receives index.html instead of JSON.
    app.router.routes.insert(0, app.router.routes.pop())
    server = uvicorn.Server(uvicorn.Config(
        app, host=settings.host, port=settings.port, log_level="info",
        timeout_graceful_shutdown=15,
    ))

    async def watch_shutdown():
        while True:
            if control.is_file() and control.read_text(encoding="utf-8") == nonce:
                server.should_exit = True
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(watch_shutdown())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    return 0 if server.started else 3


def run(arguments: argparse.Namespace) -> int:
    frontend = Path(arguments.frontend_dir).resolve()
    require((frontend / "index.html").is_file(), f"Frontend build is missing: {frontend}")
    frontend_entry = (frontend / "index.html").read_bytes()
    frontend_hash = hashlib.sha256(frontend_entry).hexdigest()
    frontend_assets = re.findall(r'<script[^>]+src="([^"]+)"', frontend_entry.decode("utf-8"))
    node = shutil.which(arguments.node)
    require(node is not None, f"Node executable not found: {arguments.node}")
    port = free_port(arguments.port)
    temporary = Path(tempfile.mkdtemp(prefix="sitehub-isolated-smoke-")).resolve()
    data = isolated_data(temporary / "data")
    nonce = secrets.token_urlsafe(24)
    base = f"http://127.0.0.1:{port}"
    report: dict[str, Any] = {
        "status": "running", "temporary_root": str(temporary), "data_dir": str(data),
        "frontend_dir": str(frontend), "frontend_entry_sha256": frontend_hash,
        "frontend_scripts": frontend_assets, "url": base, "checks": [],
    }
    print(f"[isolated smoke] temporary root: {temporary}", flush=True)
    print(f"[isolated smoke] dedicated URL: {base}", flush=True)
    env = {
        **os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "SITEHUB_DATA_DIR": str(data), "SITEHUB_FRONTEND_DIR": str(frontend),
        "SITEHUB_PROFILE_DIR": str(data / "browser-profiles"),
        "SITEHUB_ARTIFACT_DIR": str(data / "artifacts"),
        "SITEHUB_TESSERACT_CACHE": str(data / "ocr-cache"),
        "SITEHUB_DISABLE_DDDDOCR": "1", "SITEHUB_SMOKE_NONCE": nonce,
    }
    command = [
        sys.executable, "-B", str(Path(__file__).resolve()), "--serve",
        "--frontend-dir", str(frontend), "--data-dir", str(data), "--node", node,
    ]
    owned: list[OwnedProcess] = []
    server: OwnedProcess | None = None
    server_app_key: tuple[int, float] | None = None
    client = httpx.Client(base_url=base, timeout=3, trust_env=False)
    error: BaseException | None = None

    def passed(name: str, **details: Any) -> None:
        report["checks"].append({"name": name, "status": "passed", **details})
        print(f"[isolated smoke] PASS: {name}", flush=True)

    def wait_for(description: str, check, timeout: float = 35):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            for process in owned:
                process.capture()
            if server and server.child.poll() is not None:
                raise RuntimeError(f"Isolated server exited before {description}; inspect {server.log}")
            try:
                result = check()
                if result:
                    return result
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
            time.sleep(0.15)
        raise TimeoutError(f"Timed out waiting for {description}: {last_error}")

    def get(path: str) -> dict:
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def assert_identity() -> None:
        nonlocal server_app_key
        require(server is not None and matching(server.key) is not None, "Isolated server identity changed")
        current = get("/__isolated_smoke__/identity")
        require(current.get("nonce") == nonce and current.get("data_dir") == str(data),
                "Refusing an HTTP listener not owned by this smoke run")
        pid = int(current["pid"])
        if server_app_key is None:
            # Windows venv python.exe may be a redirector whose real interpreter
            # is a child process. Both must remain inside our verified tree.
            server_app_key = server.key if pid == server.key[0] else server.own_descendant(pid)
        require(pid == server_app_key[0] and matching(server_app_key) is not None,
                "Isolated HTTP interpreter creation time changed")

    try:
        server = OwnedProcess(command + ["--port", str(port)], env=env, log=temporary / "server.log")
        owned.append(server)
        wait_for("isolated server identity", lambda: get("/__isolated_smoke__/identity"))
        assert_identity()
        wait_for("health readiness", lambda: get("/api/v1/health/ready").get("status") == "ready")
        require(get("/api/v1/health/live")["status"] == "ok", "Liveness check failed")
        require(get("/api/v1/health")["status"] == "healthy", "Health check failed")
        passed("health/live/ready with real background services", server_pid=server_app_key[0], launcher_pid=server.key[0])

        other_port = free_port()
        while other_port == port:
            other_port = free_port()
        second = OwnedProcess(command + ["--port", str(other_port)], env=env, log=temporary / "second-instance.log")
        owned.append(second)
        code = second.wait(20)
        require(code != 0, "Second instance unexpectedly accepted the same data directory")
        require("another SiteHub instance already owns this data directory" in second.log.read_text(encoding="utf-8", errors="replace"),
                "Second instance failed for a reason other than the data-directory lock")
        require(second.job_active_count() == 0, "Rejected instance left child processes")
        assert_identity()
        passed("second instance rejected by the data-directory lock", rejected_pid=second.key[0])

        status = get("/api/v1/worker/status")
        old_worker = server.own_descendant(int(status["pid"]))
        worker = matching(old_worker)
        require(worker is not None, "Owned worker disappeared before the crash test")
        require(any("worker-node" in arg and "main.js" in arg for arg in worker.cmdline()),
                "Owned descendant is not the expected check-in worker")
        worker.kill()
        require(get("/api/v1/health/live")["status"] == "ok", "Control plane stopped with its worker")
        require(get("/api/v1/sites")["total"] == 0, "Isolated catalog unexpectedly contains sites")
        passed("control plane stays readable after its own worker is killed", killed_worker_pid=old_worker[0])

        def recovered_worker():
            current = get("/api/v1/worker/status")
            if (current["state"] != "ready" or not current.get("pid")
                    or int(current["restart_count"]) <= int(status["restart_count"])):
                return None
            key = server.own_descendant(int(current["pid"]))
            return current if key != old_worker and int(current["restart_count"]) > int(status["restart_count"]) else None

        recovered = wait_for("worker automatic recovery", recovered_worker)
        require(matching(old_worker) is None, "Old worker is still alive")
        wait_for("readiness after recovery", lambda: get("/api/v1/health/ready")["status"] == "ready")
        passed("worker automatically recovers", recovered_worker_pid=recovered["pid"])

        assert_identity()
        csrf = get("/api/v1/bootstrap")["csrf_token"]
        headers = {"X-CSRF-Token": csrf, "Origin": base}
        settings = get("/api/v1/settings/maintenance")
        desired = {**settings, "backup_interval_hours": 1, "backup_keep_count": 2, "retention_enabled": False}
        require(client.put("/api/v1/settings/maintenance", json=desired).status_code == 403,
                "Maintenance mutation unexpectedly bypassed CSRF")
        response = client.put("/api/v1/settings/maintenance", json=desired, headers=headers)
        response.raise_for_status()
        require(response.json() == desired, "Maintenance settings did not round-trip")
        require(get("/api/v1/settings/maintenance") == desired, "Maintenance settings were not persisted")
        response = client.post("/api/v1/maintenance/run", headers=headers, timeout=30)
        response.raise_for_status()
        maintenance = get("/api/v1/maintenance/status")
        require(maintenance["running"] is False and maintenance["last_error"] is None,
                "Maintenance did not finish cleanly")
        require((maintenance.get("latest_verification") or {}).get("integrity_check") == "ok",
                "Maintenance backup was not integrity-verified")
        passed("maintenance settings, CSRF, manual backup and status", backup_id=maintenance["latest_backup"]["backup_id"])

        require(hashlib.sha256((frontend / "index.html").read_bytes()).hexdigest() == frontend_hash,
                "Frontend build changed during smoke; rerun against a stable build")
        ui = OwnedProcess(
            [node, str(ROOT / "tools" / "ui-smoke.mjs"), "--url", base,
             "--screenshot", str(temporary / "ui-smoke.png")],
            env=env, log=temporary / "ui-smoke.log",
        )
        owned.append(ui)
        require(ui.wait(120) == 0, f"UI acceptance failed; inspect {ui.log}")
        ui_exit_deadline = time.monotonic() + 3
        while ui.job_active_count() and time.monotonic() < ui_exit_deadline:
            time.sleep(0.1)
        require(ui.job_active_count() == 0, "UI acceptance left browser child processes")
        require(hashlib.sha256((frontend / "index.html").read_bytes()).hexdigest() == frontend_hash,
                "Frontend build changed during browser acceptance; rerun against the final build")
        passed("actual browser UI acceptance", screenshot=str(temporary / "ui-smoke.png"),
               frontend_scripts=frontend_assets)
        require(get("/api/v1/health/ready")["status"] == "ready", "Service lost readiness after UI acceptance")
    except BaseException as exc:
        error = exc
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if server and matching(server.key):
            (temporary / "shutdown.request").write_text(nonce, encoding="utf-8")
            try:
                require(server.wait(35) == 0, "Isolated server did not shut down successfully")
                deadline = time.monotonic() + 3
                while server.job_active_count() and time.monotonic() < deadline:
                    time.sleep(0.1)
                require(server.job_active_count() == 0, "Graceful shutdown left owned child processes")
                passed("graceful shutdown with no owned child processes")
            except BaseException as exc:
                if error is None:
                    error = exc
                    report["error"] = f"{type(exc).__name__}: {exc}"
        elif server and error is None:
            error = RuntimeError("Isolated server exited before graceful shutdown")
            report["error"] = str(error)
        client.close()
        for process in reversed(owned):
            process.cleanup()
        remaining = [list(key) for process in owned for key in process.alive()]
        report["owned_processes"] = [
            {"pid": key[0], "create_time": key[1]}
            for key in sorted({key for process in owned for key in process.seen})
        ]
        report["remaining_processes"] = remaining
        if remaining and error is None:
            error = RuntimeError("Owned smoke processes survived final cleanup")
            report["error"] = str(error)
        report["status"] = "failed" if error else "passed"
        (temporary / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[isolated smoke] report: {temporary / 'report.json'}", flush=True)
    if error:
        raise RuntimeError(report["error"]) from error
    print("[isolated smoke] PASS: all checks completed; no owned processes remain.", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--frontend-dir", default=str(ROOT / ".pytest-runs" / "optimization-dist"))
    parser.add_argument("--port", type=int, default=0, help="Unused loopback test port; default auto-select; 8686 is forbidden")
    parser.add_argument("--node", default="node")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.serve:
        require(arguments.data_dir is not None, "Internal server needs --data-dir")
        return asyncio.run(serve(arguments))
    return run(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[isolated smoke] FAIL: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
