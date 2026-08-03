from __future__ import annotations

import asyncio
import logging
import os
import hashlib
import random
import sys
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from site_hub.jobs.service import JobService, StaleLease
from site_hub.models import JobKind, JobStatus, WorkerStatusOut
from site_hub.scrapers.network import sanitize_log_value
from site_hub.workers.jsonrpc import JsonRpcError, JsonRpcPeer


logger=logging.getLogger(__name__)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class _WindowsKillOnCloseJob:
    """Own one worker process tree and kill every descendant when closed."""

    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF
    WAIT_OBJECT_0 = 0
    WAIT_FAILED = 0xFFFFFFFF

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - production path is Windows.
            raise RuntimeError("Windows Job Objects are unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        )
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle: int | None = int(handle)
        self._process_handle: int | None = None
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            wintypes.HANDLE(self._handle), self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, pid: int) -> None:
        if self._handle is None:
            raise RuntimeError("job object is closed")
        access = (
            self.PROCESS_TERMINATE | self.PROCESS_SET_QUOTA
            | self.PROCESS_QUERY_LIMITED_INFORMATION | self.SYNCHRONIZE
        )
        process = self._kernel32.OpenProcess(access, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self._kernel32.AssignProcessToJobObject(wintypes.HANDLE(self._handle), process):
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(process)
            raise ctypes.WinError(error)
        self._process_handle = int(process)

    def wait_for_root(self) -> None:
        """Wait for the root only; inherited pipe handles in descendants cannot block this."""
        if self._process_handle is None:
            return
        result = self._kernel32.WaitForSingleObject(
            wintypes.HANDLE(self._process_handle), self.INFINITE,
        )
        if result not in {self.WAIT_OBJECT_0, self.WAIT_FAILED}:
            raise RuntimeError(f"unexpected WaitForSingleObject result: {result}")
        if result == self.WAIT_FAILED:
            error = ctypes.get_last_error()
            if error not in {0, 6}:  # Concurrent supervisor shutdown may close the handle.
                raise ctypes.WinError(error)

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle is not None and not self._kernel32.TerminateJobObject(
            wintypes.HANDLE(self._handle), exit_code,
        ):
            error = ctypes.get_last_error()
            if error not in {0, 6}:  # ERROR_INVALID_HANDLE is harmless during concurrent exit.
                raise ctypes.WinError(error)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
        if self._process_handle is not None:
            self._kernel32.CloseHandle(wintypes.HANDLE(self._process_handle))
            self._process_handle = None


class NodeWorkerSupervisor:
    def __init__(self, jobs:JobService, *, executable:str, entry:Path, profiles_dir:Path,
                 artifacts_dir:Path, handshake_seconds:int=10, ping_seconds:int=15,
                 shutdown_seconds:int=20)->None:
        self.jobs=jobs; self.executable=executable; self.entry=Path(entry)
        self.profiles_dir=profiles_dir; self.artifacts_dir=artifacts_dir
        self.handshake_seconds=handshake_seconds; self.ping_seconds=ping_seconds
        self.shutdown_seconds=shutdown_seconds
        self.state="stopped"; self.pid: int|None=None; self.instance_id: str|None=None
        self.restart_count=0; self.last_heartbeat_at:datetime|None=None; self.last_error:str|None=None
        self.adapters:list[dict[str,Any]]=[]; self._process:asyncio.subprocess.Process|None=None
        self._peer:JsonRpcPeer|None=None; self._manager:asyncio.Task[None]|None=None
        self._dispatch:asyncio.Task[None]|None=None; self._stderr:asyncio.Task[None]|None=None
        self._ready=asyncio.Event(); self._stop=asyncio.Event(); self._active_job:UUID|None=None
        self._active_lease_token:str|None=None
        self._cancel_requested_jobs:set[UUID]=set()
        self._process_job:_WindowsKillOnCloseJob|None=None
        self._crashes:deque[datetime]=deque()
        self._next_dispatch_at=0.0

    def start(self)->None:
        if not self._manager or self._manager.done():
            self._stop.clear(); self._manager=asyncio.create_task(self._manage(),name="node-worker-supervisor")

    async def stop(self)->None:
        self._stop.set(); self._ready.clear()
        if self._peer and self.state=="ready":
            with suppress(Exception): await self._peer.request("worker.shutdown",{"graceMs":self.shutdown_seconds*1000},timeout=self.shutdown_seconds)
        if self._dispatch:
            self._dispatch.cancel()
            with suppress(asyncio.CancelledError): await self._dispatch
        await self._terminate_process()
        if self._manager:
            self._manager.cancel()
            with suppress(asyncio.CancelledError): await self._manager
        self.state="stopped"

    async def restart(self)->None:
        self._crashes.clear(); await self._terminate_process()
        if not self._manager or self._manager.done(): self.start()

    def status(self)->WorkerStatusOut:
        return WorkerStatusOut(state=self.state,pid=self.pid,instance_id=self.instance_id,
            restart_count=self.restart_count,last_heartbeat_at=self.last_heartbeat_at,last_error=self.last_error)

    async def open_session(self,account_id:UUID)->dict[str,Any]: return await self._rpc("session.open",await self._session_params(account_id),60)
    async def session_status(self,account_id:UUID)->dict[str,Any]: return await self._rpc("session.status",await self._session_params(account_id),10)
    async def close_session(self,account_id:UUID)->dict[str,Any]: return await self._rpc("session.close",await self._session_params(account_id),20)
    async def cancel_job(self,job_id:UUID)->None:
        if self._active_job != job_id:
            return
        self._cancel_requested_jobs.add(job_id)
        if self._peer and self._ready.is_set():
            await self._peer.request("job.cancel",{"jobId":str(job_id)},timeout=10)

    async def _rpc(self,method:str,params:dict[str,Any],timeout:float)->dict[str,Any]:
        if not self._peer or not self._ready.is_set(): raise RuntimeError("check-in worker is not ready")
        result=await self._peer.request(method,params,timeout=timeout); return result or {}

    async def _session_params(self,account_id:UUID)->dict[str,Any]:
        account=await self.jobs.store.get_account(account_id)
        if not account: raise LookupError("check-in account not found")
        getter=getattr(self.jobs.store,"get_site_runtime",self.jobs.store.get_site)
        site=await getter(UUID(str(account["site_id"])))
        if not site: raise LookupError("site not found")
        return {"accountId":str(account_id),"site":site,
            "account":{"id":str(account_id),"name":account["name"],"adapter":account["adapter"]},
            "checkinConfig":account.get("runtime_config") or {},
            "profileDir":str(self._profile_path(str(account["profile_key"])))}

    async def _manage(self)->None:
        delay=1
        while not self._stop.is_set():
            if not self.entry.is_file():
                self.state="disabled"; self.last_error=f"Node worker entry not found: {self.entry}"
                await asyncio.sleep(5); continue
            if self._circuit_open():
                self.state="circuit_open"; await asyncio.sleep(30); continue
            try:
                await self._spawn_and_monitor(); delay=1
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.last_error=self._redact(str(exc))[:500]; self.state="degraded"
                logger.error("Node worker failed: %s", self.last_error)
            if not self._stop.is_set():
                self.restart_count+=1; self._crashes.append(datetime.now(UTC)); await asyncio.sleep(delay); delay=min(delay*2,30)

    async def _spawn_and_monitor(self)->None:
        self.state="starting"; self._ready.clear(); self.instance_id=str(uuid4())
        self._process=await asyncio.create_subprocess_exec(self.executable,str(self.entry),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            cwd=str(self.entry.parent.parent),env={**os.environ,"SITEHUB_WORKER":"1",
                "SITEHUB_DATA_DIR":str(self.profiles_dir.parent),"SITEHUB_PROFILE_DIR":str(self.profiles_dir),
                "SITEHUB_ARTIFACT_DIR":str(self.artifacts_dir),"SITEHUB_PYTHON":sys.executable})
        self.pid=self._process.pid
        if sys.platform == "win32":
            process_job = _WindowsKillOnCloseJob()
            try:
                process_job.assign(self.pid)
            except Exception:
                process_job.close()
                self._process.terminate()
                await self._process.wait()
                raise
            self._process_job = process_job
        assert self._process.stdout and self._process.stdin and self._process.stderr
        self._peer=JsonRpcPeer(self._process.stdout,self._process.stdin)
        self._peer.on("worker.hello",self._on_hello); self._peer.on("worker.ready",self._on_ready)
        self._peer.on("job.heartbeat",self._on_job_heartbeat); self._peer.on("job.complete",self._on_job_complete)
        self._peer.on("session.event",self._on_session_event); self._peer.start()
        self._stderr=asyncio.create_task(self._drain_stderr(self._process.stderr))
        ping:asyncio.Task[None]|None=None
        try:
            await asyncio.wait_for(self._ready.wait(),self.handshake_seconds)
            self.state="ready"; self.last_error=None
            self._dispatch=asyncio.create_task(self._dispatch_loop(),name="node-checkin-dispatch")
            ping=asyncio.create_task(self._ping_loop(),name="node-worker-ping")
            process_job = self._process_job
            if process_job:
                await asyncio.to_thread(process_job.wait_for_root)
                # Closing the kill-on-close job at root exit removes any browser/helper
                # descendants before asyncio waits for inherited stdout/stderr handles.
                self._close_process_job()
            await self._process.wait()
            if self._active_job and self._active_lease_token:
                with suppress(StaleLease):
                    await self.jobs.complete(self._active_job,self._active_lease_token,
                        status=JobStatus.EFFECT_UNKNOWN,error_kind="worker_lost",
                        error_message="check-in worker exited while the outcome was unknown")
            raise RuntimeError(f"Node worker exited with code {self._process.returncode}")
        finally:
            self._ready.clear(); self.pid=None; self._active_job=None; self._active_lease_token=None
            if ping:
                ping.cancel()
                with suppress(asyncio.CancelledError): await ping
            if self._dispatch:
                self._dispatch.cancel()
                with suppress(asyncio.CancelledError): await self._dispatch
                self._dispatch=None
            self._close_process_job()
            if self._peer: await self._peer.close()
            if self._stderr:
                self._stderr.cancel();
                with suppress(asyncio.CancelledError): await self._stderr

    async def _on_hello(self,params:dict[str,Any])->dict[str,Any]:
        if int(params.get("protocol",0))!=1: raise JsonRpcError(-32001,"unsupported protocol")
        self.adapters=list(params.get("adapters") or [])
        return {"protocol":1,"instanceId":self.instance_id,"profilesDir":str(self.profiles_dir),"artifactsDir":str(self.artifacts_dir),"maxConcurrency":1}
    async def _on_ready(self,params:dict[str,Any])->dict[str,Any]: self._ready.set(); self.last_heartbeat_at=datetime.now(UTC); return {"accepted":True}
    async def _on_job_heartbeat(self,params:dict[str,Any])->dict[str,Any]:
        self.last_heartbeat_at=datetime.now(UTC)
        ok=await self.jobs.heartbeat(UUID(params["jobId"]),str(params["leaseToken"]),{"phase":params.get("phase")})
        return {"accepted":ok}
    async def _on_job_complete(self,params:dict[str,Any])->dict[str,Any]:
        job_id=UUID(params["jobId"])
        cancel_requested=job_id in self._cancel_requested_jobs
        status={"success":JobStatus.SUCCEEDED,"skipped":JobStatus.SKIPPED,"failed":JobStatus.FAILED,"effect_unknown":JobStatus.EFFECT_UNKNOWN,"cancelled":JobStatus.CANCELLED}.get(str(params.get("status")),JobStatus.FAILED)
        result={"checkin_status":params.get("status"),"reason_code":params.get("reasonCode"),"message":self._redact(str(params.get("message") or ""))[:500],"duration_ms":params.get("durationMs"),"artifact_count":len(params.get("artifacts") or []),"metadata":params.get("metadata") or {}}
        diagnostic_status = status in {JobStatus.FAILED, JobStatus.EFFECT_UNKNOWN}
        try:
            await self.jobs.complete(job_id,str(params["leaseToken"]),status=status,result=result,
                error_kind=params.get("reasonCode") if diagnostic_status else None,
                error_message=self._redact(str(params.get("message") or ""))[:500] if diagnostic_status else None)
        except StaleLease:
            if cancel_requested:
                return {"accepted":False,"stale":True,"cancelRequested":True}
            raise JsonRpcError(-32010,"STALE_LEASE")
        finally:
            self._active_job=None; self._active_lease_token=None
            self._cancel_requested_jobs.discard(job_id)
            self._next_dispatch_at=asyncio.get_running_loop().time()+random.SystemRandom().uniform(3,12)
        for artifact in await self._persist_artifacts(job_id,params.get("artifacts") or []):
            await self.jobs.events.publish("artifact",artifact)
        return {"accepted":True}
    async def _on_session_event(self,params:dict[str,Any])->dict[str,Any]:
        account_id=params.get("accountId")
        if account_id and hasattr(self.jobs.store,"set_account_auth_state"):
            with suppress(Exception):
                await self.jobs.store.set_account_auth_state(
                    UUID(str(account_id)),str(params.get("state") or "error"),params.get("message")
                )
        await self.jobs.events.publish("session",params); return {"accepted":True}

    async def _dispatch_loop(self)->None:
        assert self.instance_id
        while self._ready.is_set() and not self._stop.is_set():
            delay=self._next_dispatch_at-asyncio.get_running_loop().time()
            if delay>0: await asyncio.sleep(min(delay,.5)); continue
            if self._active_job: await asyncio.sleep(.5); continue
            job=await self.jobs.lease(JobKind.CHECKIN,self.instance_id)
            if not job: await asyncio.sleep(.5); continue
            self._active_job=UUID(str(job["id"])); token=str(job["lease_token"])
            self._active_lease_token=token
            try:
                await self.jobs.mark_running(UUID(str(job["id"])),token)
                account=await self.jobs.store.get_account(UUID(str(job["account_id"])))
                getter=getattr(self.jobs.store,"get_site_runtime",self.jobs.store.get_site)
                site=await getter(UUID(str(job["site_id"])))
                if (
                    not account or not site
                    or site.get("status") != "active"
                    or not bool(account.get("enabled"))
                ):
                    await self.jobs.complete(
                        UUID(str(job["id"])),token,status=JobStatus.SKIPPED,
                        result={
                            "reason_code":"disabled",
                            "message":"check-in skipped because the site, account, capability, or schedule is disabled",
                        },
                    )
                    self._active_job=None; self._active_lease_token=None
                    self._next_dispatch_at=asyncio.get_running_loop().time()+random.SystemRandom().uniform(3,12)
                    continue
                if self._active_job in self._cancel_requested_jobs:
                    raise StaleLease("check-in cancellation was requested before worker start")
                params={"jobId":str(job["id"]),"leaseToken":token,"accountId":str(account["id"]),
                    "deadlineAt":(datetime.now(UTC)+timedelta(minutes=10)).isoformat(),"site":site,
                    "account":{"id":account["id"],"name":account["name"],"adapter":account["adapter"]},
                    "checkinConfig":account.get("runtime_config") or account.get("config") or {},
                    "profileDir":str(self._profile_path(str(account["profile_key"]))),
                    "reconcile":int(job.get("attempt") or 1)>1}
                response=await self._peer.request("job.start",params,timeout=10) if self._peer else None
                if not response or not response.get("accepted"): raise RuntimeError("worker rejected job")
            except StaleLease:
                self._cancel_requested_jobs.discard(UUID(str(job["id"])))
                self._active_job=None; self._active_lease_token=None
            except Exception as exc:
                self._cancel_requested_jobs.discard(UUID(str(job["id"])))
                self._active_job=None; self._active_lease_token=None
                with suppress(StaleLease): await self.jobs.complete(UUID(str(job["id"])),token,status=JobStatus.FAILED,error_kind="infrastructure",error_message=self._redact(str(exc))[:500])

    async def _ping_loop(self)->None:
        while self._ready.is_set():
            await asyncio.sleep(self.ping_seconds)
            await self._rpc("worker.ping",{},max(3,self.ping_seconds-1)); self.last_heartbeat_at=datetime.now(UTC)

    async def _drain_stderr(self,reader:asyncio.StreamReader)->None:
        while line:=await reader.readline(): logger.info("[node] %s",self._redact(line.decode(errors="replace").rstrip()))

    def _profile_path(self,relative:str)->Path:
        root=self.profiles_dir.resolve(); target=(root/relative).resolve()
        if target!=root and root not in target.parents: raise ValueError("profile path escaped its controlled root")
        return target

    async def _persist_artifacts(self,job_id:UUID,items:list[dict[str,Any]])->list[dict[str,Any]]:
        saved=[]
        root=self.artifacts_dir.resolve()
        for item in items:
            relative=str(item.get("path") or item.get("relativePath") or item.get("relative_path") or "")
            if not relative: continue
            target=(root/relative).resolve()
            if target==root or root not in target.parents or not target.is_file():
                logger.warning("worker returned an invalid artifact path for job %s",job_id); continue
            digest,size=await asyncio.to_thread(self._hash_file,target)
            supplied=str(item.get("sha256") or "").lower()
            if supplied and supplied!=digest:
                logger.warning("worker artifact checksum mismatch for job %s",job_id); continue
            supplied_size=item.get("sizeBytes") or item.get("size")
            if supplied_size is not None and int(supplied_size)!=size:
                logger.warning("worker artifact size mismatch for job %s",job_id); continue
            saved.append(await self.jobs.store.create_artifact({"job_id":job_id,"kind":item.get("kind") or item.get("role") or "screenshot",
                "relative_path":target.relative_to(root).as_posix(),"sha256":digest,"size":size,
                "mime_type":item.get("mimeType") or item.get("mime_type")}))
        return saved

    @staticmethod
    def _hash_file(path:Path)->tuple[str,int]:
        digest=hashlib.sha256(); size=0
        with path.open("rb") as handle:
            while chunk:=handle.read(1024*1024): digest.update(chunk); size+=len(chunk)
        return digest.hexdigest(),size

    @staticmethod
    def _redact(value:str)->str:
        return sanitize_log_value(value, max_length=2000)
    def _circuit_open(self)->bool:
        cutoff=datetime.now(UTC)-timedelta(minutes=5)
        while self._crashes and self._crashes[0]<cutoff: self._crashes.popleft()
        return len(self._crashes)>=5
    async def _terminate_process(self)->None:
        process=self._process; self._process=None
        process_job=self._process_job; self._process_job=None
        try:
            if not process:
                return
            if process.returncode is None:
                if process_job:
                    process_job.terminate()
                    process_job.close()
                    process_job=None
                else:
                    process.terminate()
                try: await asyncio.wait_for(process.wait(),5)
                except TimeoutError: process.kill(); await process.wait()
        finally:
            if process_job:
                process_job.close()

    def _close_process_job(self)->None:
        process_job=self._process_job; self._process_job=None
        if process_job:
            process_job.close()
