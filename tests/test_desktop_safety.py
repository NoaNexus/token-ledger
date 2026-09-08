from __future__ import annotations

import socket
import time
from pathlib import Path

from tokenledger.api import TokenLedgerServer, bind_local_server, local_server_url
from tokenledger.config import AppConfig
from tokenledger.desktop import _fit_window_geometry, _start_scan_scheduler


def _config(tmp_path: Path, port: int) -> AppConfig:
    return AppConfig(
        user_home=tmp_path / "synthetic-home",
        data_dir=tmp_path / "data",
        web_dir=tmp_path / "web",
        host="127.0.0.1",
        port=port,
        open_browser=False,
    )


def test_server_does_not_reuse_an_active_listener() -> None:
    assert TokenLedgerServer.allow_reuse_address is False


def test_bind_local_server_uses_ephemeral_port_when_requested_port_is_occupied(tmp_path: Path) -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    occupied_port = occupied.getsockname()[1]
    server = bind_local_server(_config(tmp_path, occupied_port), object(), object())
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] != occupied_port
        assert local_server_url(server) == f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.server_close()
        occupied.close()


def test_desktop_no_longer_contains_process_termination_fallback() -> None:
    import tokenledger.desktop as desktop

    assert not hasattr(desktop, "_free_port_if_stale")


def test_scan_scheduler_can_be_stopped_without_scanning_real_home() -> None:
    class SyntheticScanner:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def start_background(self, force: bool = False) -> bool:
            self.calls.append(force)
            return True

    scanner = SyntheticScanner()
    stop_event, thread = _start_scan_scheduler(scanner, initial_delay=0, interval=0.02, force=True)
    deadline = time.monotonic() + 1
    while not scanner.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert scanner.calls and scanner.calls[0] is True

    stop_event.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    call_count = len(scanner.calls)
    time.sleep(0.05)
    assert len(scanner.calls) == call_count


def test_window_geometry_stays_inside_available_work_area() -> None:
    assert _fit_window_geometry(0, 0, 1366, 728) == (24, 24, 1318, 680)
    assert _fit_window_geometry(0, 0, 1920, 1080) == (240, 80, 1440, 920)
