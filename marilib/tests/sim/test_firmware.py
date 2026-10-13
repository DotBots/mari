import json

import pytest

from marilib.mari_protocol import MARI_GATEWAY_INFO_VERSION, MARI_PROTOCOL_VERSION
from marilib.model import MARI_SLOT_DURATION_MS, SCHEDULES
from marilib.sim.firmware import (
    SNAPSHOT_PATH,
    Firmware,
    FirmwareSourceError,
    find_firmware_dir,
    load_firmware,
    parse_firmware,
)

FIRMWARE_DIR = find_firmware_dir()
needs_sources = pytest.mark.skipif(FIRMWARE_DIR is None, reason="firmware sources not available")


@needs_sources
@pytest.mark.parametrize(
    "name, schedule_id, n_cells, max_nodes",
    [("tiny", 6, 17, 10), ("medium", 4, 67, 44), ("big", 3, 101, 66), ("huge", 1, 149, 102)],
)
def test_schedules_match_the_firmware(name, schedule_id, n_cells, max_nodes):
    schedule = parse_firmware(FIRMWARE_DIR).schedule(name)
    assert (schedule.id, schedule.n_cells, schedule.max_nodes) == (schedule_id, n_cells, max_nodes)
    assert schedule.slots.count("U") == max_nodes
    assert schedule.slots.startswith("BBB")


@needs_sources
def test_slot_and_protocol_constants_match_marilib():
    firmware = parse_firmware(FIRMWARE_DIR)
    assert firmware.slot_us == 1780
    assert firmware.slot_us / 1000 == MARI_SLOT_DURATION_MS
    assert firmware["MARI_PROTOCOL_VERSION"] == MARI_PROTOCOL_VERSION
    assert firmware["MARI_GATEWAY_INFO_VERSION"] == MARI_GATEWAY_INFO_VERSION


@needs_sources
def test_cell_order_matches_the_marilib_table():
    firmware = parse_firmware(FIRMWARE_DIR)
    for schedule_id, entry in SCHEDULES.items():
        assert firmware.schedule(schedule_id).slots == entry["slots"]


@needs_sources
def test_snapshot_is_current():
    """Regenerate with `python -m marilib.sim.snapshot --write` when this fails."""
    assert json.loads(SNAPSHOT_PATH.read_text()) == parse_firmware(FIRMWARE_DIR).to_json()


def test_snapshot_loads_without_sources():
    firmware = Firmware.from_json(json.loads(SNAPSHOT_PATH.read_text()), "snapshot")
    assert firmware.slot_us == 1780
    assert firmware.schedule("huge").max_nodes == 102


def test_derived_timeouts():
    firmware = load_firmware()
    assert firmware["MARI_JOINING_STATE_TIMEOUT"] == firmware.slot_us * 3 // 2
    assert firmware["MARI_SCAN_MAX_SLOTS"] == 149


@pytest.mark.parametrize(
    "n, expected",
    [
        (1, "tiny"),
        (10, "tiny"),
        (11, "medium"),
        (44, "medium"),
        (45, "big"),
        (66, "big"),
        (67, "huge"),
        (102, "huge"),
    ],
)
def test_schedule_for_picks_the_smallest_that_fits(n, expected):
    assert load_firmware().schedule_for(n).name == expected


def test_schedule_for_rejects_too_many_nodes():
    with pytest.raises(ValueError):
        load_firmware().schedule_for(103)


def test_explicit_directory_without_sources_is_an_error(tmp_path):
    with pytest.raises(FirmwareSourceError):
        find_firmware_dir(tmp_path)
