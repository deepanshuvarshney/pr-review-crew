"""Keep the suite offline and away from the repo cache."""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRCREW_CACHE_DIR", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(self: socket.socket, address: object) -> None:  # noqa: ANN001
        raise AssertionError(f"network blocked: {address}")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
