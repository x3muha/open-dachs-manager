"""Single-owner serial worker and queued Unix-socket client.

The worker is the only process which opens the optical MSR2 adapter.  Every
accepted Unix-socket connection becomes one FIFO queue item and owns the
physical session until it closes.  This keeps multi-step operations such as
authentication plus write plus readback atomic while allowing short-lived
CLI, TUI and web sessions to take turns safely.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
import json
import os
from pathlib import Path
import queue
import signal
import socket
import stat
import threading
import time
from typing import Callable

from .transport import BlockResult, Frame, Response, SerialSession, TransportError


DEFAULT_SERIAL_WORKER_SOCKET = "/run/open-dachs-manager/serial.sock"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 128 * 1024


def _frame_to_dict(frame: Frame | None) -> dict | None:
    if frame is None:
        return None
    return {
        "kind": frame.kind,
        "packet": frame.packet,
        "raw_hex": frame.raw.hex(),
        "payload_hex": frame.payload.hex(),
        "positive": frame.positive,
    }


def _frame_from_dict(item: dict | None) -> Frame | None:
    if item is None:
        return None
    return Frame(
        str(item["kind"]),
        int(item["packet"]),
        bytes.fromhex(str(item.get("raw_hex") or "")),
        bytes.fromhex(str(item.get("payload_hex") or "")),
        item.get("positive"),
    )


def _response_to_dict(response: Response) -> dict:
    return {
        "tx_hex": response.tx.hex(),
        "ack": _frame_to_dict(response.ack),
        "data": _frame_to_dict(response.data),
        "elapsed_ms": response.elapsed_ms,
        "crc_errors": response.crc_errors,
        "protocol_errors": response.protocol_errors,
    }


def _response_from_dict(item: dict) -> Response:
    return Response(
        bytes.fromhex(str(item.get("tx_hex") or "")),
        _frame_from_dict(item.get("ack")),
        _frame_from_dict(item.get("data")),
        float(item.get("elapsed_ms") or 0.0),
        int(item.get("crc_errors") or 0),
        int(item.get("protocol_errors") or 0),
    )


def _block_to_dict(result: BlockResult) -> dict:
    return {
        "block": result.block,
        "cpu": result.cpu,
        "packet": result.packet,
        "response": _response_to_dict(result.response),
        "status": result.status,
        "payload_hex": result.payload.hex(),
    }


def _block_from_dict(item: dict) -> BlockResult:
    return BlockResult(
        int(item["block"]),
        int(item["packet"]),
        _response_from_dict(dict(item["response"])),
        item.get("status"),
        bytes.fromhex(str(item.get("payload_hex") or "")),
        int(item.get("cpu", 0)),
    )


class SerialWorkerSession:
    """SerialSession-compatible client for the FIFO worker."""

    def __init__(self, socket_path: str | Path = DEFAULT_SERIAL_WORKER_SOCKET,
                 queue_timeout: float = 120.0):
        if queue_timeout <= 0:
            raise ValueError("serial worker queue timeout must be greater than zero")
        self.socket_path = str(socket_path)
        self.queue_timeout = float(queue_timeout)
        self.port = f"worker:{self.socket_path}"
        self._socket: socket.socket | None = None
        self._file = None
        self._request_id = 0

    def __enter__(self) -> "SerialWorkerSession":
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self.queue_timeout)
        try:
            client.connect(self.socket_path)
        except OSError as exc:
            client.close()
            raise TransportError(f"cannot connect to serial worker {self.socket_path}: {exc}") from exc
        self._socket = client
        self._file = client.makefile("rwb")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.is_open:
            try:
                self._rpc("close")
            except Exception:
                pass
        if self._file is not None:
            self._file.close()
        if self._socket is not None:
            self._socket.close()
        self._file = None
        self._socket = None

    @property
    def is_open(self) -> bool:
        return self._socket is not None and self._file is not None

    def _rpc(self, method: str, **parameters):
        if not self.is_open:
            raise TransportError("serial worker session is not open")
        self._request_id += 1
        request = {
            "version": PROTOCOL_VERSION,
            "id": self._request_id,
            "method": method,
            "params": parameters,
        }
        raw = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._file.write(raw)
            self._file.flush()
            response_raw = self._file.readline(MAX_MESSAGE_BYTES + 1)
        except (OSError, socket.timeout) as exc:
            raise TransportError(f"serial worker request failed or queue timed out: {exc}") from exc
        if not response_raw:
            raise TransportError("serial worker closed the connection without a response")
        if len(response_raw) > MAX_MESSAGE_BYTES:
            raise TransportError("serial worker response exceeded the protocol limit")
        try:
            response = json.loads(response_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"invalid serial worker response: {exc}") from exc
        if int(response.get("id", -1)) != self._request_id:
            raise TransportError("serial worker response id mismatch")
        if not response.get("ok"):
            error = str(response.get("error") or "unknown worker error")
            error_type = str(response.get("error_type") or "RuntimeError")
            if error_type == "ValueError":
                raise ValueError(error)
            if error_type == "TransportError":
                raise TransportError(error)
            raise RuntimeError(f"serial worker: {error}")
        return response.get("result")

    def ping(self) -> dict:
        return dict(self._rpc("ping") or {})

    def next_packet(self) -> int:
        return int(self._rpc("next_packet"))

    def request(self, payload: bytes, packet: int, timeout: float = 0.9, cpu: int = 0) -> Response:
        item = self._rpc(
            "request", payload_hex=bytes(payload).hex(), packet=int(packet),
            timeout=float(timeout), cpu=int(cpu),
        )
        return _response_from_dict(dict(item))

    def sync(self, packet: int = 0, timeout: float = 0.9, cpu: int = 0) -> Response:
        item = self._rpc("sync", packet=int(packet), timeout=float(timeout), cpu=int(cpu))
        return _response_from_dict(dict(item))

    def read_block(self, block: int, packet: int | None = None, timeout: float = 0.9,
                   cpu: int = 0) -> BlockResult:
        item = self._rpc(
            "read_block", block=int(block), packet=packet, timeout=float(timeout), cpu=int(cpu)
        )
        return _block_from_dict(dict(item))

    def write_block(self, block: int, payload: bytes, packet: int | None = None,
                    timeout: float = 0.9, cpu: int = 0) -> Response:
        item = self._rpc(
            "write_block",
            block=int(block),
            payload_hex=bytes(payload).hex(),
            packet=packet,
            timeout=float(timeout),
            cpu=int(cpu),
        )
        return _response_from_dict(dict(item))


class SerialWorkerServer:
    """FIFO Unix-socket server with one persistent physical serial owner."""

    def __init__(self, socket_path: str | Path, port: str, baud: int = 19200,
                 read_timeout: float = 0.02, queue_size: int = 128,
                 client_idle_timeout: float = 30.0,
                 session_factory: Callable[[], SerialSession] | None = None):
        if queue_size < 1:
            raise ValueError("queue size must be at least one")
        self.socket_path = Path(socket_path)
        self.port = str(port)
        self.baud = int(baud)
        self.read_timeout = float(read_timeout)
        self.client_idle_timeout = float(client_idle_timeout)
        self.session_factory = session_factory or (
            lambda: SerialSession(self.port, self.baud, self.read_timeout)
        )
        self.jobs: queue.Queue[tuple[float, socket.socket] | None] = queue.Queue(queue_size)
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.listener: socket.socket | None = None
        self.worker_thread: threading.Thread | None = None
        self.physical_session: SerialSession | None = None
        self.started_at = time.time()
        self.connections = 0
        self.requests = 0
        self.last_error: str | None = None

    def _open_physical(self) -> SerialSession:
        if self.physical_session is None or not self.physical_session.is_open:
            session = self.session_factory()
            self.physical_session = session.__enter__()
        return self.physical_session

    def _close_physical(self) -> None:
        if self.physical_session is not None:
            try:
                self.physical_session.__exit__(None, None, None)
            finally:
                self.physical_session = None

    def _status(self, queue_wait_ms: float) -> dict:
        return {
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "socket": str(self.socket_path),
            "port": self.port,
            "serial_open": bool(self.physical_session and self.physical_session.is_open),
            "queue_depth": self.jobs.qsize(),
            "queue_wait_ms": round(queue_wait_ms, 1),
            "connections": self.connections,
            "requests": self.requests,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "last_error": self.last_error,
        }

    def _dispatch(self, method: str, params: dict, queue_wait_ms: float):
        if method == "ping":
            return self._status(queue_wait_ms)
        session = self._open_physical()
        if method == "next_packet":
            return session.next_packet()
        if method == "request":
            return _response_to_dict(session.request(
                bytes.fromhex(str(params.get("payload_hex") or "")),
                int(params["packet"]),
                float(params.get("timeout", 0.9)),
                int(params.get("cpu", 0)),
            ))
        if method == "sync":
            return _response_to_dict(session.sync(
                int(params.get("packet", 0)), float(params.get("timeout", 0.9)),
                int(params.get("cpu", 0)),
            ))
        if method == "read_block":
            packet = params.get("packet")
            return _block_to_dict(session.read_block(
                int(params["block"]),
                None if packet is None else int(packet),
                float(params.get("timeout", 0.9)),
                int(params.get("cpu", 0)),
            ))
        if method == "write_block":
            packet = params.get("packet")
            return _response_to_dict(session.write_block(
                int(params["block"]),
                bytes.fromhex(str(params.get("payload_hex") or "")),
                None if packet is None else int(packet),
                float(params.get("timeout", 0.9)),
                int(params.get("cpu", 0)),
            ))
        raise ValueError(f"unsupported serial worker method: {method}")

    @staticmethod
    def _send(stream, payload: dict) -> None:
        stream.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        stream.flush()

    def _serve_client(self, accepted_at: float, connection: socket.socket) -> None:
        queue_wait_ms = (time.monotonic() - accepted_at) * 1000.0
        connection.settimeout(self.client_idle_timeout)
        with connection, connection.makefile("rwb") as stream:
            while not self.stop_event.is_set():
                try:
                    raw = stream.readline(MAX_MESSAGE_BYTES + 1)
                except (OSError, socket.timeout):
                    return
                if not raw:
                    return
                request_id = None
                try:
                    if len(raw) > MAX_MESSAGE_BYTES:
                        raise ValueError("serial worker request exceeded the protocol limit")
                    request = json.loads(raw.decode("utf-8"))
                    request_id = request.get("id")
                    if int(request.get("version", -1)) != PROTOCOL_VERSION:
                        raise ValueError("unsupported serial worker protocol version")
                    method = str(request.get("method") or "")
                    if method == "close":
                        self._send(stream, {"id": request_id, "ok": True, "result": True})
                        return
                    self.requests += 1
                    result = self._dispatch(method, dict(request.get("params") or {}), queue_wait_ms)
                    self._send(stream, {"id": request_id, "ok": True, "result": result})
                except Exception as exc:
                    self.last_error = str(exc)
                    if isinstance(exc, TransportError):
                        self._close_physical()
                    try:
                        self._send(stream, {
                            "id": request_id,
                            "ok": False,
                            "error_type": exc.__class__.__name__,
                            "error": str(exc),
                        })
                    except OSError:
                        return

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            job = self.jobs.get()
            if job is None:
                break
            accepted_at, connection = job
            self.connections += 1
            try:
                self._serve_client(accepted_at, connection)
            except OSError as exc:
                # A web/client process may disappear while its buffered socket
                # file is being closed (notably during a coordinated systemd
                # restart).  That ends only this FIFO lease and must not kill
                # the worker thread or produce a shutdown traceback.
                if not self.stop_event.is_set():
                    self.last_error = str(exc)
                with suppress(OSError):
                    connection.close()
        self._close_physical()

    def _remove_stale_socket(self) -> None:
        if not os.path.lexists(self.socket_path):
            return
        mode = self.socket_path.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            raise RuntimeError(f"refusing to replace non-socket path: {self.socket_path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            self.socket_path.unlink()
        except OSError as exc:
            raise RuntimeError(f"cannot verify existing serial worker socket: {exc}") from exc
        else:
            raise RuntimeError(f"serial worker is already running at {self.socket_path}")
        finally:
            probe.close()

    def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._remove_stale_socket()
        self._open_physical()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener = listener
        try:
            listener.bind(str(self.socket_path))
        except Exception:
            listener.close()
            self.listener = None
            self._close_physical()
            raise
        os.chmod(self.socket_path, 0o660)
        listener.listen(128)
        listener.settimeout(0.5)
        self.worker_thread = threading.Thread(target=self._worker_loop, name="dachs-serial-fifo", daemon=True)
        self.worker_thread.start()
        self.ready_event.set()
        try:
            while not self.stop_event.is_set():
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                self.jobs.put((time.monotonic(), connection))
        finally:
            self.stop()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
            self.listener = None
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        if self.worker_thread is not None and self.worker_thread is not threading.current_thread():
            self.worker_thread.join(timeout=5.0)
        self._close_physical()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-dachs-serial-worker",
        description="Queued single-owner Dachs MSR2 serial worker",
    )
    parser.add_argument("--socket", default=os.environ.get("OPEN_DACHS_SERIAL_SOCKET", DEFAULT_SERIAL_WORKER_SOCKET))
    parser.add_argument("--port", default=os.environ.get("OPEN_DACHS_SERIAL_PORT", "/dev/ttyUSB0"))
    parser.add_argument("--baud", type=int, default=int(os.environ.get("OPEN_DACHS_BAUD", "19200")))
    parser.add_argument("--read-timeout", type=float, default=0.02)
    parser.add_argument("--queue-size", type=int, default=128)
    parser.add_argument("--client-idle-timeout", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = SerialWorkerServer(
        args.socket,
        args.port,
        args.baud,
        args.read_timeout,
        args.queue_size,
        args.client_idle_timeout,
    )

    def stop_worker(_signum, _frame):
        server.stop()

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    print(f"Open Dachs Serialworker: {args.socket} -> {args.port} @ {args.baud}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
