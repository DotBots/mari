"""Schedules and protocol constants, read out of the mari firmware sources.

The simulator never carries a hand-copied protocol constant. Every number it
models the MAC with is parsed from `firmware/mari/` at load time, or, when
marilib is installed without the repo, from `firmware_snapshot.json`, which is
written by `python -m marilib.sim.snapshot` and checked
against the sources by the test suite.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

FIRMWARE_DIR_ENV = "MARI_FIRMWARE_DIR"
SNAPSHOT_PATH = Path(__file__).with_name("firmware_snapshot.json")

# Where each constant is defined. The parser evaluates the macro the way the
# preprocessor would, so a value derived from other macros stays derived.
SOURCE_FILES = ("models.h", "mac.h", "packet.h", "queue.h", "scan.h", "association.c")
CONSTANTS = (
    "MARI_WHOLE_SLOT_DURATION",
    "MARI_MAX_SLOTFRAMES_NO_RX_LEAVE",
    "MARI_SCAN_MAX_SLOTS",
    "MARI_BACKOFF_N_MIN",
    "MARI_BACKOFF_N_MAX",
    "MARI_BACKOFF_MAX_STREAK",
    "MARI_JOIN_TIMEOUT_SINCE_SYNCED",
    "MARI_JOINING_STATE_TIMEOUT",
    "MARI_PACKET_QUEUE_SIZE",
    "MARI_N_BLE_REGULAR_CHANNELS",
    "MARI_N_BLE_ADVERTISING_CHANNELS",
    "MARI_PROTOCOL_VERSION",
    "MARI_GATEWAY_INFO_VERSION",
    "MARI_AUTO_UPLINK_KEEPALIVE",
)
FLAGS = ("MARI_ENABLE_BEACON_HOPPING",)

BEACON, SHARED_UPLINK, DOWNLINK, UPLINK = "B", "S", "D", "U"
CELL_TYPES = (BEACON, SHARED_UPLINK, DOWNLINK, UPLINK)


class FirmwareSourceError(RuntimeError):
    """The firmware sources are missing or do not parse the way this module expects."""


@dataclass(frozen=True)
class Cell:
    type: str
    channel_offset: int


@dataclass(frozen=True)
class Schedule:
    name: str
    id: int
    max_nodes: int
    cells: tuple[Cell, ...]

    @property
    def n_cells(self) -> int:
        return len(self.cells)

    @property
    def slots(self) -> str:
        return "".join(cell.type for cell in self.cells)

    def indices(self, cell_type: str) -> tuple[int, ...]:
        return tuple(i for i, cell in enumerate(self.cells) if cell.type == cell_type)


@dataclass(frozen=True)
class Firmware:
    constants: dict[str, int]
    flags: dict[str, bool]
    schedules: dict[str, Schedule]
    source: str  # the firmware directory, or "snapshot"

    def __getitem__(self, name: str) -> int:
        return self.constants[name]

    @property
    def slot_us(self) -> int:
        return self.constants["MARI_WHOLE_SLOT_DURATION"]

    def schedule(self, name_or_id: str | int) -> Schedule:
        for schedule in self.schedules.values():
            if name_or_id in (schedule.name, schedule.id):
                return schedule
        raise KeyError(f"no mari schedule {name_or_id!r} (have {', '.join(self.schedules)})")

    def schedule_for(self, n_nodes: int) -> Schedule:
        """The smallest schedule with room for `n_nodes`, as an operator would pick it."""
        for schedule in sorted(self.schedules.values(), key=lambda s: s.max_nodes):
            if n_nodes <= schedule.max_nodes:
                return schedule
        largest = max(s.max_nodes for s in self.schedules.values())
        raise ValueError(f"no mari schedule fits {n_nodes} nodes (largest holds {largest})")

    def to_json(self) -> dict:
        return {
            "constants": dict(sorted(self.constants.items())),
            "flags": dict(sorted(self.flags.items())),
            "schedules": {
                schedule.name: {
                    "id": schedule.id,
                    "max_nodes": schedule.max_nodes,
                    "cells": [[cell.type, cell.channel_offset] for cell in schedule.cells],
                }
                for schedule in sorted(self.schedules.values(), key=lambda s: s.max_nodes)
            },
        }

    @classmethod
    def from_json(cls, data: dict, source: str) -> "Firmware":
        schedules = {
            name: Schedule(
                name=name,
                id=entry["id"],
                max_nodes=entry["max_nodes"],
                cells=tuple(Cell(type_, offset) for type_, offset in entry["cells"]),
            )
            for name, entry in data["schedules"].items()
        }
        return cls(dict(data["constants"]), dict(data["flags"]), schedules, source)


# ------------------------------------------------------------------ sources --


def find_firmware_dir(explicit: str | os.PathLike | None = None) -> Path | None:
    """The `firmware/mari` directory: explicit path, then $MARI_FIRMWARE_DIR, then this repo.

    An explicit path or env var that holds no sources is an error, not a reason
    to fall back to the snapshot.
    """
    for base in (explicit, os.environ.get(FIRMWARE_DIR_ENV) or None):
        if base is None:
            continue
        for candidate in (Path(base), Path(base) / "mari", Path(base) / "firmware" / "mari"):
            if all((candidate / name).is_file() for name in SOURCE_FILES):
                return candidate.resolve()
        raise FirmwareSourceError(f"{base} does not hold the mari firmware sources")
    in_repo = Path(__file__).resolve().parents[3] / "firmware" / "mari"
    return in_repo if all((in_repo / name).is_file() for name in SOURCE_FILES) else None


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def _strip_comments(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text))


_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)(?:[ \t]+(.*?))?[ \t]*$", re.M)
_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_INT_LITERAL = re.compile(r"\b(0[xX][0-9a-fA-F]+|\d+)[uUlL]*\b")
_ARITHMETIC = re.compile(r"[0-9xXa-fA-F+\-*/%()<>\s]+")
_BUILTINS = {"UINT8_MAX": "255"}


class _Macros:
    def __init__(self, text: str):
        self.defines = dict(_BUILTINS)
        for name, value in _DEFINE.findall(text):
            self.defines.setdefault(name, value.strip())

    def is_defined(self, name: str) -> bool:
        return name in self.defines

    def value(self, name: str, stack: tuple[str, ...] = ()) -> int:
        if name not in self.defines:
            raise FirmwareSourceError(f"macro {name} is not defined")
        if name in stack:
            raise FirmwareSourceError(f"macro {name} expands to itself")
        expression = _INT_LITERAL.sub(lambda m: str(int(m.group(1), 0)), self.defines[name])
        if not expression:
            raise FirmwareSourceError(f"macro {name} is a flag, not a value")
        expression = _IDENT.sub(
            lambda m: f"({self.value(m.group(0), stack + (name,))})", expression
        )
        if not _ARITHMETIC.fullmatch(expression):
            raise FirmwareSourceError(f"{name} is not integer arithmetic: {expression!r}")
        # every operand is a non-negative integer, so floor division is C's division
        return int(eval(re.sub(r"/", "//", expression), {"__builtins__": {}}, {}))  # noqa: S307


_SCHEDULE = re.compile(r"schedule_t\s+schedule_(\w+)\s*=\s*\{(.*?)\n\}\s*;", re.S)
_FIELD = re.compile(r"\.(\w+)\s*=\s*(\d+)")
_CELL = re.compile(r"\{\s*'([A-Z])'\s*,\s*(\d+)\s*,")


def _parse_schedules(text: str) -> dict[str, Schedule]:
    schedules = {}
    for name, body in _SCHEDULE.findall(text):
        fields = {key: int(value) for key, value in _FIELD.findall(body)}
        cells = tuple(Cell(type_, int(offset)) for type_, offset in _CELL.findall(body))
        missing = {"id", "max_nodes", "n_cells"} - fields.keys()
        if missing:
            raise FirmwareSourceError(f"schedule_{name} has no {', '.join(sorted(missing))}")
        if len(cells) != fields["n_cells"]:
            raise FirmwareSourceError(
                f"schedule_{name}: .n_cells is {fields['n_cells']}, found {len(cells)} cells"
            )
        if unknown := {cell.type for cell in cells} - set(CELL_TYPES):
            raise FirmwareSourceError(f"schedule_{name}: unknown cell types {sorted(unknown)}")
        schedules[name] = Schedule(name, fields["id"], fields["max_nodes"], cells)
    if not schedules:
        raise FirmwareSourceError("no schedule_t definitions in all_schedules.c")
    return schedules


def parse_firmware(firmware_dir: Path) -> Firmware:
    text = "\n".join(_strip_comments((firmware_dir / name).read_text()) for name in SOURCE_FILES)
    macros = _Macros(text)
    return Firmware(
        constants={name: macros.value(name) for name in CONSTANTS},
        flags={name: macros.is_defined(name) for name in FLAGS},
        schedules=_parse_schedules(_strip_comments((firmware_dir / "all_schedules.c").read_text())),
        source=str(firmware_dir),
    )


@lru_cache(maxsize=None)
def _load(explicit: str | None) -> Firmware:
    firmware_dir = find_firmware_dir(explicit)
    if firmware_dir is None:
        return Firmware.from_json(json.loads(SNAPSHOT_PATH.read_text()), "snapshot")
    return parse_firmware(firmware_dir)


def load_firmware(firmware_dir: str | os.PathLike | None = None) -> Firmware:
    """The firmware model: from the sources when they can be found, else the snapshot."""
    return _load(None if firmware_dir is None else str(firmware_dir))
