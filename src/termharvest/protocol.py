"""Length-safe line/frame protocol used between client and mock terminal."""

from __future__ import annotations

import socket
from typing import Protocol

from .errors import E_CONNECTION_CLOSED, E_FRAME_MALFORMED, E_FRAME_TIMEOUT, E_FRAME_TOO_LARGE, HarvestError
from .screen import MAX_FRAME_BYTES

FRAME_TERMINATOR = b"\x1e"
KEY_TERMINATOR = b"\n"
MAX_KEY_BYTES = 64

class ReceivingSocket(Protocol):
    def recv(self, size: int) -> bytes: ...

class FrameReader:
    """Incrementally read terminator-delimited UTF-8 frames.

    The buffer survives across calls, so a single ``recv`` containing two
    frames (coalescing) does not lose the second one.
    """

    def __init__(self, sock: ReceivingSocket, max_bytes: int = MAX_FRAME_BYTES) -> None:
        self.sock = sock
        self.max_bytes = max_bytes
        self.buffer = b""

    def read_frame(self) -> str:
        while True:
            marker = self.buffer.find(FRAME_TERMINATOR)
            if marker >= 0:
                if marker > self.max_bytes:
                    raise HarvestError(E_FRAME_TOO_LARGE, f"frame exceeds {self.max_bytes} bytes", context={"size": marker, "limit": self.max_bytes})
                payload = self.buffer[:marker]
                self.buffer = self.buffer[marker + len(FRAME_TERMINATOR) :]
                try:
                    return payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HarvestError(E_FRAME_MALFORMED, "frame is not valid UTF-8", context={"byte_offset": exc.start}, cause=exc) from exc
            if len(self.buffer) > self.max_bytes:
                raise HarvestError(E_FRAME_TOO_LARGE, f"frame exceeds {self.max_bytes} bytes without terminator", context={"size": len(self.buffer), "limit": self.max_bytes})
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, TimeoutError) as exc:
                raise HarvestError(E_FRAME_TIMEOUT, "timed out waiting for a frame", cause=exc) from exc
            except OSError as exc:
                raise HarvestError(E_CONNECTION_CLOSED, "socket failed while waiting for a frame", cause=exc) from exc
            if not chunk:
                raise HarvestError(E_CONNECTION_CLOSED, "connection closed before frame terminator", context={"buffered": len(self.buffer)})
            self.buffer += chunk

def encode_key(token: str) -> bytes:
    if not isinstance(token, str) or not token:
        raise HarvestError(E_FRAME_MALFORMED, "key token must be non-empty text")
    if "\n" in token or "\r" in token or "\x1e" in token:
        raise HarvestError(E_FRAME_MALFORMED, "key token must not contain protocol delimiters")
    encoded = token.encode("ascii", errors="strict")
    if len(encoded) > MAX_KEY_BYTES:
        raise HarvestError(E_FRAME_TOO_LARGE, "key token is too long", context={"limit": MAX_KEY_BYTES})
    return encoded + KEY_TERMINATOR

def send_key(sock: socket.socket, token: str) -> None:
    payload = encode_key(token)
    try:
        sock.sendall(payload)
    except OSError as exc:
        raise HarvestError(E_CONNECTION_CLOSED, "failed to send key token", cause=exc) from exc

def wire_frame(text: str) -> bytes:
    """Encode a frame for the mock server; keeps the encoding in one place."""
    return text.encode("utf-8") + FRAME_TERMINATOR
