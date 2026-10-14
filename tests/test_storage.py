import pytest

from sportflex.core import storage
from sportflex.core.storage import RoutineStore, SnipeStore, WatchStore


@pytest.mark.parametrize(
    "store",
    [
        lambda d: SnipeStore("xindian", "me", d),
        lambda d: RoutineStore("xindian", "me", d),
        lambda d: WatchStore("me", d),
    ],
)
def test_save_round_trips_and_leaves_no_temp_file(tmp_path, store):
    item = store(tmp_path)
    item.save({"targetDate": "2026-10-28", "targets": [{"name": "場A"}]})
    assert item.load()["targets"] == [{"name": "場A"}]
    assert [path.name for path in tmp_path.iterdir()] == [item.path.name]


def test_a_crash_mid_write_keeps_the_previous_file(tmp_path, monkeypatch):
    item = SnipeStore("xindian", "me", tmp_path)
    item.save({"targetDate": "2026-10-28", "phase": "armed"})

    def crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(storage.os, "replace", crash)
    with pytest.raises(OSError):
        item.save({"targetDate": "2026-10-29"})
    assert item.load() == {"targetDate": "2026-10-28"}
