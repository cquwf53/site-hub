from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any
from uuid import uuid4


JsonHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message); self.code = code; self.data = data


class JsonRpcPeer:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 *, max_line_bytes: int = 1024 * 1024) -> None:
        self.reader = reader; self.writer = writer; self.max_line_bytes = max_line_bytes
        self.handlers: dict[str, JsonHandler] = {}
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()

    def on(self, method: str, handler: JsonHandler) -> None: self.handlers[method] = handler

    def start(self) -> None:
        if not self._reader_task:
            self._reader_task = asyncio.create_task(self._read_loop(), name="node-jsonrpc-reader")

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      *, timeout: float = 15) -> Any:
        request_id = f"p-{uuid4()}"
        future = asyncio.get_running_loop().create_future(); self.pending[request_id] = future
        await self._send({"jsonrpc":"2.0","id":request_id,"method":method,"params":params or {}})
        try: return await asyncio.wait_for(future, timeout)
        finally: self.pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc":"2.0","method":method,"params":params or {}})

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError): await self._reader_task
        if not self.writer.is_closing():
            self.writer.close()
            with suppress(Exception): await self.writer.wait_closed()
        self._fail_pending(ConnectionError("JSON-RPC peer closed")); self.closed.set()

    async def _send(self, message: dict[str, Any]) -> None:
        encoded=(json.dumps(message, ensure_ascii=False, separators=(",",":"), default=str)+"\n").encode()
        if len(encoded)>self.max_line_bytes: raise ValueError("JSON-RPC frame too large")
        async with self._write_lock:
            self.writer.write(encoded); await self.writer.drain()

    async def _read_loop(self) -> None:
        try:
            while True:
                line=await self.reader.readline()
                if not line: break
                if len(line)>self.max_line_bytes: raise JsonRpcError(-32700,"JSON-RPC frame too large")
                try: message=json.loads(line)
                except json.JSONDecodeError: continue
                if not isinstance(message,dict) or message.get("jsonrpc")!="2.0": continue
                if "method" in message: asyncio.create_task(self._handle_request(message))
                elif "id" in message: self._handle_response(message)
        finally:
            self._fail_pending(ConnectionError("JSON-RPC peer disconnected")); self.closed.set()

    async def _handle_request(self, message: dict[str, Any]) -> None:
        request_id=message.get("id"); method=str(message.get("method")); params=message.get("params") or {}
        handler=self.handlers.get(method)
        try:
            if not handler: raise JsonRpcError(-32601,f"unknown method: {method}")
            result=await handler(params)
            if request_id is not None: await self._send({"jsonrpc":"2.0","id":request_id,"result":result})
        except JsonRpcError as exc:
            if request_id is not None:
                await self._send({"jsonrpc":"2.0","id":request_id,"error":{"code":exc.code,"message":str(exc),"data":exc.data}})
        except Exception as exc:
            if request_id is not None:
                await self._send({"jsonrpc":"2.0","id":request_id,"error":{"code":-32603,"message":str(exc)}})

    def _handle_response(self,message:dict[str,Any])->None:
        future=self.pending.get(str(message["id"]))
        if not future or future.done(): return
        if "error" in message:
            error=message["error"] or {}; future.set_exception(JsonRpcError(int(error.get("code",-32000)),str(error.get("message","RPC error")),error.get("data")))
        else: future.set_result(message.get("result"))

    def _fail_pending(self,error:Exception)->None:
        for future in tuple(self.pending.values()):
            if not future.done(): future.set_exception(error)
