from __future__ import annotations

import socket

import pytest

from termharvest.errors import HarvestError
from termharvest.protocol import FRAME_TERMINATOR, FrameReader, encode_key, send_key, wire_frame

class FakeSocket:
    def __init__(self, chunks, *, timeout=False):
        self.chunks = list(chunks)
        self.timeout = timeout
        self.sent = b""

    def recv(self, _size):
        if self.timeout:
            raise socket.timeout("synthetic timeout")
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def sendall(self, payload):
        self.sent += payload

def test_partial_frame_is_reassembled():
    reader = FrameReader(FakeSocket([b"first ", b"half", b" second\x1e"]))
    assert reader.read_frame() == "first half second"

def test_coalesced_frames_are_both_returned():
    reader = FrameReader(FakeSocket([b"one\x1etwo\x1e"]))
    assert reader.read_frame() == "one"
    assert reader.read_frame() == "two"

def test_malformed_utf8_is_rejected():
    reader = FrameReader(FakeSocket([b"\xff\xfe\x1e"]))
    with pytest.raises(HarvestError) as caught:
        reader.read_frame()
    assert caught.value.code == "E_FRAME_MALFORMED"

def test_oversized_frame_is_rejected():
    reader = FrameReader(FakeSocket([b"x" * 11]), max_bytes=10)
    with pytest.raises(HarvestError) as caught:
        reader.read_frame()
    assert caught.value.code == "E_FRAME_TOO_LARGE"

def test_timeout_maps_to_stable_error():
    reader = FrameReader(FakeSocket([], timeout=True))
    with pytest.raises(HarvestError) as caught:
        reader.read_frame()
    assert caught.value.code == "E_FRAME_TIMEOUT"

def test_closed_connection_before_terminator_is_reported():
    reader = FrameReader(FakeSocket([b"partial"]))
    with pytest.raises(HarvestError) as caught:
        reader.read_frame()
    assert caught.value.code == "E_CONNECTION_CLOSED"

def test_key_encoding_and_sending():
    assert encode_key("PF7") == b"PF7\n"
    assert encode_key("ENTER") == b"ENTER\n"
    fake = FakeSocket([])
    send_key(fake, "PF3")
    assert fake.sent == b"PF3\n"

@pytest.mark.parametrize("token", ["", "A\nB", "A\x1eB"])
def test_invalid_key_tokens_rejected(token):
    with pytest.raises(HarvestError) as caught:
        encode_key(token)
    assert caught.value.code == "E_FRAME_MALFORMED"

def test_wire_frame_uses_record_separator():
    payload = wire_frame("screen text")
    assert payload == b"screen text" + FRAME_TERMINATOR
    assert payload.endswith(b"\x1e")
