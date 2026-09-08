# mari + marilib (unified)

Unified repository: **Mari** TSCH-over-BLE link-layer firmware
(`firmware/`) + the **marilib** Python control plane and agents
(`marilib/`). Both halves are self-contained — each owns its own
build config, tests, and examples. CI is path-filtered so a
Python-only change doesn't trigger firmware builds and vice versa.

## Layout

```
firmware/      ← C, nRF52840/nRF5340, SES + emBuild — see firmware/AGENTS.md
marilib/       ← Python package + tests + examples  — see marilib/AGENTS.md
README.md      ← repo overview (both halves)
AGENTS.md      ← this file (cross-cutting)
LICENSE        ← Apache 2.0 (covers both halves)
.gitignore     ← merged (firmware + Python patterns)
.pre-commit-config.yaml  ← merged (clang-format + hatch fmt + isort)
.github/workflows/       ← path-filtered: firmware/** vs marilib/**
```

## Build

| Half | Build cmd |
|---|---|
| Firmware | `make -C firmware docker` (CI path), or `make -C firmware node|gateway` with SES locally |
| marilib | `cd marilib && pip install -e .`; `cd marilib && hatch test` |

## Cross-half coupling

- **Wire protocol**: `marilib` talks to a UART-attached Mari gateway
  over HDLC. Framing contract:
  `marilib/marilib/serial_hdlc.py` ↔ `firmware/app/03app_gateway_app/hdlc.[ch]`.
  Both halves must agree on framing and on `MetricsProbePayload`
  layout (`marilib/marilib/mari_protocol.py` ↔
  `firmware/mari/models.h`).
- **`marilib_timestamp` field** in
  `firmware/app/03app_node/main.c` — legacy shared-struct artifact
  slated for removal (see consolidation roadmap in
  `dotbot-testbed/AGENTS.md`).

## Branch policy

- Default branch: `main`, and it is the trunk. Releases are tags on it.
- Feature branches off `main`; PRs target `main`. There is no integration branch.

## Don't

- Don't change paths that submodule consumers (currently only
  `swarmit`) traverse without bumping their submodule pointer and
  updating internal path references.
- Half-specific don'ts live in `firmware/AGENTS.md` and
  `marilib/AGENTS.md`.
