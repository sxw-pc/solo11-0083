from __future__ import annotations

import socket

import pytest

from termharvest.errors import HarvestError
from termharvest.mockhost import MockTerminalServer
from termharvest.mock_scenario import load_scenario
from termharvest.protocol import FrameReader, send_key
from termharvest.screen import ScreenFrame

def recv_screen(connection, reader):
    return ScreenFrame.parse(reader.read_frame())

def test_mock_rejects_non_loopback_bind(demo_paths):
    with pytest.raises(HarvestError) as caught:
        MockTerminalServer(demo_paths["demo"], host="0.0.0.0", port=0)
    assert caught.value.code == "E_TARGET_NOT_ALLOWED"

def test_demo_scenario_navigation(mock_server):
    connection = socket.create_connection(mock_server.address, timeout=2.0)
    connection.settimeout(2.0)
    reader = FrameReader(connection)
    try:
        menu = recv_screen(connection, reader)
        assert "MAIN MENU" in menu.text
        send_key(connection, "C")
        customer = recv_screen(connection, reader)
        assert "CUSTOMER PROFILE" in customer.text
        assert "C00042" in customer.text
        send_key(connection, "ENTER")
        page1 = recv_screen(connection, reader)
        assert "PAGE 1 OF 2" in page1.text
        send_key(connection, "PF7")
        page2 = recv_screen(connection, reader)
        assert "PAGE 2 OF 2" in page2.text
        assert "END OF LIST" in page2.text
    finally:
        connection.close()

def test_once_mode_stops_after_one_connection(demo_paths):
    server = MockTerminalServer(demo_paths["demo"], port=0, frame_timeout=1.0, once=True)
    server.start()
    try:
        connection = socket.create_connection(server.address, timeout=2.0)
        reader = FrameReader(connection)
        assert "MAIN MENU" in ScreenFrame.parse(reader.read_frame()).text
        connection.close()
        server.wait(timeout=2.0)
        assert server._thread is not None
        assert not server._thread.is_alive()
    finally:
        server.stop()

def test_timeout_behavior_drops_frames(demo_paths):
    server = MockTerminalServer(demo_paths["error"], port=0, frame_timeout=0.15)
    server.start()
    connection = socket.create_connection(server.address, timeout=2.0)
    connection.settimeout(0.15)
    reader = FrameReader(connection)
    try:
        assert "MAIN MENU" in ScreenFrame.parse(reader.read_frame()).text
        send_key(connection, "TIMEOUT")
        with pytest.raises(HarvestError) as caught:
            reader.read_frame()
        assert caught.value.code == "E_FRAME_TIMEOUT"
    finally:
        connection.close()
        server.stop()

def test_scenario_rejects_bad_dimensions(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"scenario_id":"bad","start_screen":"x","screens":' '{"x":{"rows":["short"],"transitions":{}}}}', encoding="utf-8")
    with pytest.raises(HarvestError) as caught:
        load_scenario(path)
    assert caught.value.code == "E_SCENARIO_INVALID"

def test_paged_scenario_serves_three_pages(demo_paths):
    scenario = load_scenario(demo_paths["paged"])
    assert scenario.next_screen("bill_list_1", "PF7") == "bill_list_2"
    assert scenario.next_screen("bill_list_3", "PF7") == "bill_list_3"
    assert "B000206" in scenario.frame("bill_list_3").text
