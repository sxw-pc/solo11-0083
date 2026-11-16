"""Deterministic loopback-only mock character terminal."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

from .config_util import is_loopback_host
from .errors import E_CONNECTION_FAILED, E_TARGET_NOT_ALLOWED, HarvestError
from .mock_scenario import MockScenario, load_scenario
from .protocol import wire_frame

MAX_KEY_LINE = 64

class MockTerminalServer:
    """A read-only screen server that rejects non-loopback bindings."""

    def __init__(
        self, scenario: MockScenario | str | Path, *, host: str = "127.0.0.1", port: int = 0, max_connections: int = 8, frame_timeout: float = 5.0, once: bool = False, logger: Any = None
    ) -> None:
        if not is_loopback_host(host):
            raise HarvestError(E_TARGET_NOT_ALLOWED, "mock terminal may bind only to loopback", context={"host": host})
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise HarvestError(E_CONNECTION_FAILED, "port must be 0..65535", context={"port": port})
        if max_connections < 1:
            raise HarvestError(E_CONNECTION_FAILED, "max_connections must be >= 1")
        if frame_timeout <= 0:
            raise HarvestError(E_CONNECTION_FAILED, "frame_timeout must be > 0")
        self.scenario = load_scenario(scenario) if isinstance(scenario, (str, Path)) else scenario
        self.host = host
        self.port = port
        self.max_connections = max_connections
        self.frame_timeout = frame_timeout
        self.once = once
        self.logger = logger
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connections = 0
        self._lock = threading.Lock()
        self._client_threads: list[threading.Thread] = []

    @property
    def actual_port(self) -> int:
        if self._listener is None:
            return 0
        return int(self._listener.getsockname()[1])

    @property
    def address(self) -> tuple[str, int]:
        host = "127.0.0.1" if self.host.rstrip(".").lower() == "localhost" else self.host
        return host, self.actual_port

    def _bind(self) -> None:
        bind_host = "127.0.0.1" if self.host.strip().lower() == "localhost" else self.host
        family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((bind_host, self.port))
            listener.listen(max(1, self.max_connections))
            listener.settimeout(0.2)
        except OSError as exc:
            listener.close()
            raise HarvestError(E_CONNECTION_FAILED, "mock terminal could not bind", context={"host": bind_host, "port": self.port}, cause=exc) from exc
        self._listener = listener

    def start(self) -> int:
        if self._listener is not None:
            return self.actual_port
        self._bind()
        self._thread = threading.Thread(target=self._accept_loop, name="termharvest-mock", daemon=True)
        self._thread.start()
        if self.logger is not None:
            self.logger.info("event=mock_ready scenario=%s host=%s port=%s", self.scenario.scenario_id, self.address[0], self.actual_port)
        return self.actual_port

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        for thread in list(self._client_threads):
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._listener is not None:
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self._connections >= self.max_connections:
                    connection.close()
                    break
                self._connections += 1
                index = self._connections
            if self.logger is not None:
                self.logger.info("event=mock_connection index=%s", index)
            if self.once:
                self._handle(connection)
                self.stop()
                break
            thread = threading.Thread(target=self._handle, args=(connection,), daemon=True)
            self._client_threads.append(thread)
            thread.start()

    def _read_key(self, connection: socket.socket) -> str | None:
        buffer = b""
        while b"\n" not in buffer:
            try:
                chunk = connection.recv(128)
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                return None
            buffer += chunk
            if len(buffer) > MAX_KEY_LINE:
                return None
        line = buffer.split(b"\n", 1)[0]
        try:
            return line.decode("ascii").strip()
        except UnicodeDecodeError:
            return None

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(self.frame_timeout)
        current = self.scenario.start_screen
        try:
            connection.sendall(wire_frame(self.scenario.frame(current).text))
            while not self._stop.is_set():
                key = self._read_key(connection)
                if key is None:
                    break
                if key in self.scenario.drop_keys:
                    if self.logger is not None:
                        self.logger.info("event=mock_drop key=%s", key)
                    continue
                delay = self.scenario.delay_ms.get(key, 0)
                if delay:
                    time.sleep(delay / 1000.0)
                next_screen = self.scenario.next_screen(current, key)
                current = next_screen
                connection.sendall(wire_frame(self.scenario.frame(current).text))
                if self.logger is not None:
                    self.logger.info("event=mock_frame key=%s screen=%s", key, current)
        except OSError:
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

def serve_mock(scenario: str | Path, *, host: str = "127.0.0.1", port: int = 0, max_connections: int = 8, frame_timeout: float = 5.0, once: bool = False, logger: Any = None) -> int:
    server = MockTerminalServer(scenario, host=host, port=port, max_connections=max_connections, frame_timeout=frame_timeout, once=once, logger=logger)
    server.start()
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0
