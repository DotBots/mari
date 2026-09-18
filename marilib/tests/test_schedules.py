"""Tests for the fixed-schedule table.

The vectors are read out of the firmware tables in firmware/mari/all_schedules.c
and the slot timing out of firmware/mari/mac.h, so a schedule changed on one side
and not the other fails here rather than on the bench.
"""

import re
from pathlib import Path

import pytest

from marilib.model import MARI_SLOT_DURATION_MS, SCHEDULES

FIRMWARE_DIR = Path(__file__).resolve().parents[2] / "firmware" / "mari"

pytestmark = pytest.mark.skipif(
    not FIRMWARE_DIR.is_dir(),
    reason="firmware sources not available (marilib installed without the repo)",
)


def _without_comments(source: str) -> str:
    lines = (re.sub(r"//.*$", "", line) for line in source.splitlines())
    return "\n".join(line for line in lines if line.strip())


def _firmware_schedules() -> dict:
    """The schedules the firmware compiles in, keyed by schedule id."""
    source = _without_comments((FIRMWARE_DIR / "all_schedules.c").read_text())
    schedules = {}
    for match in re.finditer(r"schedule_t schedule_(\w+) = \{(.*?)\n\};", source, re.S):
        body = match.group(2)

        def field(name, body=body):
            return int(re.search(rf"\.{name}\s*=\s*(\d+)", body).group(1))

        schedules[field("id")] = {
            "name": match.group(1),
            "slots": "".join(re.findall(r"\{'(\w)'", body)),
            "max_nodes": field("max_nodes"),
            "n_cells": field("n_cells"),
        }
    return schedules


def _firmware_slot_duration_us() -> float:
    """MARI_WHOLE_SLOT_DURATION, expanded from the mac.h defines it is built from."""
    source = _without_comments((FIRMWARE_DIR / "mac.h").read_text())
    macros = dict(re.findall(r"#define\s+(\w+)\s+(\(.*?\)|\w+)\s*$", source, re.M))
    macros["UINT8_MAX"] = "255"

    def expand(name, depth=0):
        assert depth < 10, f"macro expansion of {name} does not terminate"
        expression = macros[name]
        return re.sub(
            r"[A-Z_][A-Z0-9_]*",
            lambda m: f"({expand(m.group(0), depth + 1)})",
            expression,
        )

    return eval(expand("MARI_WHOLE_SLOT_DURATION"))  # noqa: S307


FIRMWARE_SCHEDULES = _firmware_schedules() if FIRMWARE_DIR.is_dir() else {}


def test_slot_duration_matches_the_firmware():
    assert _firmware_slot_duration_us() / 1000.0 == MARI_SLOT_DURATION_MS


def test_every_firmware_schedule_is_in_the_table():
    assert sorted(SCHEDULES) == sorted(FIRMWARE_SCHEDULES)


@pytest.mark.parametrize("schedule_id", sorted(FIRMWARE_SCHEDULES))
def test_schedule_matches_the_firmware(schedule_id):
    schedule = SCHEDULES[schedule_id]
    firmware = FIRMWARE_SCHEDULES[schedule_id]
    assert schedule["name"] == firmware["name"]
    assert schedule["slots"] == firmware["slots"]
    assert len(schedule["slots"]) == firmware["n_cells"]
    assert schedule["max_nodes"] == firmware["max_nodes"]


@pytest.mark.parametrize("schedule_id", sorted(SCHEDULES))
def test_counts_and_duration_follow_the_slots(schedule_id):
    schedule = SCHEDULES[schedule_id]
    assert schedule["d_down"] == schedule["slots"].count("D")
    assert schedule["max_nodes"] == schedule["slots"].count("U")
    assert schedule["sf_duration"] == round(len(schedule["slots"]) * MARI_SLOT_DURATION_MS, 2)
