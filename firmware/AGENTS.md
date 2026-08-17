# firmware (mari — TSCH-over-BLE link layer)

Firmware half of the unified `mari` repo. See `../AGENTS.md` for
the cross-cutting view and `../marilib/AGENTS.md` for the Python
half.

## Purpose

TSCH-over-BLE link-layer firmware for dense low-power IoT/robot
networks. Driving use case: the 1,000-DotBot OpenSwarm testbed.
Single gateway supports up to 102 nodes with ~270 ms RTT and >97%
PDR; horizontal scalability via multiple uncoordinated gateways
with node-driven handovers (<50 ms single-node, ~0.34 s avg for
100 moving nodes). No link-layer ACKs (latency tradeoff — apps
must retry). No security yet (planned EDHOC/ELA/PSK integration).

## Tech stack

- **Language**: C
- **Targets**: nRF52833, nRF52840, nRF5340 (dual-core; gateway runs
  on net core)
- **Build**: SEGGER Embedded Studio (`.emProject`) via `emBuild`
  from `Makefile`; Docker image `aabadie/dotbot:latest` wraps SES
  for CI
- **Style**: `clang-format` v15, `pre-commit` (root config covers
  both halves)
- **No package manager**

## Entry points

- `mari/mari.h` — public API surface (`mari_init`, node/gateway
  functions)
- `mari/mac.c` — TSCH MAC state machine, slot timing
- `app/03app_node/main.c` — minimal node example showing intended
  integration

## Build / run / test

```bash
# from firmware/
make node                                    # nrf52840dk node
make gateway                                 # nRF5340 net-core gateway
make all                                     # default
make docker                                  # CI path
```

One host test, for the gateway's HDLC decoder. From
`app/03app_gateway_app/`, `cc -o test_hdlc test_hdlc.c hdlc.c &&
./test_hdlc` (regenerate its vectors from marilib's encoder with
`PYTHONPATH=../../../marilib python3 test_hdlc_vectors.py >
test_hdlc_vectors.h`). Everything else is on-target: `app/01mari_*` are
experimental apps, not a unit-test suite, and CI only verifies the
firmware compiles.

## Flashing the bench (`flash.sh`)

Flashes over SWD with `nrfjprog`. Flash-only by default; `--build`
compiles first. Run with a role and no targets to list what is
connected.

```bash
SEGGER_DIR=/opt/segger ./flash.sh <node|gateway|both> --all|<snr...>
```

- Roles map to families: `node` is nRF52840
  (`03app_node`), `gateway` is the dual-core nRF5340
  (`03app_gateway_app` + `03app_gateway_net`). `both` runs the
  gateway pass then the node pass with the same options, so
  `both --all --build` does a whole bench in one command.
- **A run only touches its own family**, and boards of the other
  family are reported separately, so a missing board is
  distinguishable from one plugged in under the wrong role.
  Flashing nodes cannot wipe a gateway. `--force` overrides.
- Locked (APPROTECT) boards read as UNKNOWN and are auto-recovered.
  The gateway is recovered on both cores every flash unless
  `--no-recover`.
- `--schedule <tiny|medium|big|huge>` flashes the net core from the
  `build-schedules.sh` cache, and refuses an image older than the
  sources (add `--build` to refresh just that one, `--force` to
  flash it anyway).
- `--net-id <hex>` writes the network id into the net core's config
  page after programming, which is what makes one image usable on
  any network. Nodes have no such page: theirs is
  `MARI_APP_NET_ID` in `03app_node/main.c`.
- `--erase-only` recovers and stops. `--dry-run` prints every
  `nrfjprog` command without running it.

## Building the schedule images (`build-schedules.sh`)

The gateway's schedule is a **compile-time** pointer
(`schedule_app` in `03app_gateway_net/main.c`), so changing it
means a rebuild. Nodes adopt whatever the beacon advertises, so
only the gateway is ever reflashed for a schedule change.

Building takes minutes and flashing takes seconds, so build once
and flash from the cache during a campaign:

```bash
./build-schedules.sh              # all four -> Output/schedules/
./build-schedules.sh tiny huge    # just these
./flash.sh gateway --all --schedule tiny
```

It edits `main.c` in place and restores it byte-for-byte via an
EXIT trap, including through a failure. **Uncommitted edits are
preserved, not discarded** - a bench tree normally carries the
local net id, so that is the common case, not the exception.

## Cross-half / cross-repo coupling

- **`marilib`** (now in this repo at `../marilib/`): wire-level
  protocol agreement via HDLC and `MetricsProbePayload`. Field
  `marilib_timestamp` in `app/03app_node/main.c` is a legacy
  shared-struct artifact slated for removal.
- **`swarmit`** (layering inversion):
  - `drv/mr_rng/mr_rng_nrf5340_app.c` — `extern swarmit_init_rng` /
    `swarmit_read_rng`
  - `drv/mr_timer_hf/mr_timer_hf.c` — skips TIMER4 handler under
    swarmit's `USE_BULK_UART`
- No git submodules.

## Hot spots and known gaps

- **No link-layer ACKs and no security/encryption**. Cleanest
  greenfield work item; bolting on either touches `mac.c`,
  `packet.c`/`packet.h`, `models.h`.
- **No automated tests**: no unit tests, no host-side simulation,
  no hardware-in-loop.
- **Code-smell debt visible on the integration branch**: multiple
  "FIXME: remove before merge" and "only for debugging" comments
  shipped (`association.c`, `scheduler.c`); two `// TODO: use PPI
  instead` in the hot timing path (`mac.c`) — real perf/jitter
  wins available.
- **Strong SES lock-in**: vendored Nordic startup/linker assets
  under `nRF/`, seven `.emProject` files at this dir.

## Don't

- **Don't change slot timing constants** (`TsTxOffset`,
  `GuardTime`, slot duration 1.7 ms) without re-running the
  join-storm and handover experiments documented in the Mari paper.
- **Don't add link-layer ACKs casually** — the current latency
  profile depends on their absence. Any reliability addition should
  be optional and per-frame.
- **Don't refactor the Bloom filter** membership encoding without
  verifying the false-positive rate stays under 5% at 100 nodes per
  gateway.
