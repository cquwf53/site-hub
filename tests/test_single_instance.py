from __future__ import annotations

from pathlib import Path

import pytest

from site_hub.services.single_instance import SingleInstanceError, SingleInstanceLock


def test_single_instance_rejects_same_data_directory_and_releases(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path / "data")
    second = SingleInstanceLock(tmp_path / "data")
    first.acquire()
    try:
        with pytest.raises(SingleInstanceError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_single_instance_allows_independent_data_directories(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path / "one")
    second = SingleInstanceLock(tmp_path / "two")
    first.acquire()
    try:
        second.acquire()
        second.release()
    finally:
        first.release()
