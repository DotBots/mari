# Mari 💫 👀 🐍

Mari is a lightweight wireless connectivity solution designed for dense IoT networks, with a focus on supporting real-time interactions and fast over-the-air (OTA) updates.

The driving use case for the design of Mari is the OpenSwarm Testbed of 1,000 [DotBots](https://github.com/DotBots/DotBot-firmware). Mari is suitable for any dense IoT deployment requiring low-latency communication.

Mari has been validated at scale: in January 2026, a single Mari network carried **724 mobile nodes** across 8 independent gateways. The `mari-cloud` TUI below monitors that deployment live, with about 102 nodes per gateway at roughly 270 ms latency.

![The mari-cloud terminal UI monitoring a 724-node Mari network: a summary line reporting the network ID, 8 gateways, 724 nodes and radio PDR, above per-gateway panels listing node count, schedule, latency, PDR and the addresses of the nodes attached to each gateway.](assets/mari-cloud-tui-724-nodes.png)

This repository ships **two halves**:

- [`firmware/`](firmware/) — the C firmware that implements the Mari TSCH-over-BLE link layer on Nordic nRF52840 / nRF5340.
- [`marilib/`](marilib/) — the Python control plane (`mari-edge`, `mari-cloud` CLI agents) that talks to a Mari gateway over UART or MQTT and renders a Rich-based real-time TUI.

Each half is self-contained: its build config, tests, and examples live in its own directory.

## Key Features

- **TSCH over BLE**: Time-Synchronized Channel Hopping (TSCH) over Bluetooth Low Energy 2 Mbps PHY
- **Multi-Gateway Architecture**: scale by adding more gateways
- **Non-Coordinated Gateways**: gateways are independent — infrastructure setup is simple
- **Fast Handovers**: quick transitions between gateways as nodes move
- **Low-Power Operation**: energy-efficient for battery-powered devices
- **Real-Time Communication**: 100–150 ms average latency with 100 nodes per gateway
- **Reasonable Throughput for OTAP**: about 10 Kb/s downlink
- **Quick Network Join**: 150 ms best-case, 6 s worst-case
- **Dense Network Support**: scales for hundreds to thousands of nodes

## Layout

```
mari/
├── firmware/       # C firmware (mari TSCH-over-BLE link layer)
│   ├── app/        # Applications and tests
│   ├── drv/        # Hardware drivers
│   ├── mari/       # Core protocol implementation
│   └── nRF/        # Nordic Semiconductor SDK files
└── marilib/        # Python package + tests + examples
    ├── marilib/    # The importable Python package
    ├── tests/
    ├── examples/
    ├── pyproject.toml
    └── install_marilib.sh
```

## Firmware (mari)

### Hardware support

Validated on Nordic nRF52833, nRF52840, and nRF5340 (dual-core; gateway on the network core).

### Build

```bash
cd firmware
make node        # nrf52840dk node
make gateway     # nRF5340 net-core gateway
make all         # default
make docker      # CI path (Docker-wrapped SEGGER ES)
```

The project uses SEGGER Embedded Studio `.emProject` files, `clang-format` v15, and `pre-commit` (root config).

### Flash

`firmware/flash.sh` programs connected boards over SWD with `nrfjprog`. It flashes without compiling by default; pass `--build` to build first. Run it with a role and no targets to list what is connected.

```bash
cd firmware
./flash.sh node --all                 # every connected nRF52840 node
./flash.sh gateway <snr>              # one nRF5340 gateway, by J-Link serial
./flash.sh both --all --build         # build and flash the whole bench
```

A run only touches its own family (`node` is nRF52840, `gateway` is the dual-core nRF5340), and boards of the other family are listed separately, so flashing nodes cannot wipe a gateway and a board plugged in under the wrong role is easy to spot. Boards that ship with readback protection are recovered automatically.

Useful options: `--net-id <hex>` provisions the gateway's network id into flash after programming (so one image works on any network), `--erase-only` recovers the targets without programming, and `--dry-run` prints the `nrfjprog` commands without running them. `./flash.sh --help` is the full list.

### Schedules

The gateway's schedule is chosen at **compile time** (`schedule_app` in `app/03app_gateway_net/main.c`), so switching it needs a rebuild. Nodes do not: they adopt whatever the beacon advertises, so only the gateway is reflashed.

Since building takes minutes and flashing takes seconds, `build-schedules.sh` builds one gateway net-core image per schedule up front, and `flash.sh --schedule` flashes from that cache:

```bash
cd firmware
./build-schedules.sh                  # tiny, medium, big, huge -> Output/schedules/
./flash.sh gateway --all --schedule tiny
```

`--schedule` refuses an image older than the firmware sources; add `--build` to refresh that one image. `build-schedules.sh` restores `main.c` byte-for-byte when it finishes or fails, so local uncommitted edits survive the build.

### Example usage

```c
// Gateway init
mari_init(MARI_GATEWAY, MARI_NET_ID_DEFAULT, schedule, event_callback);

// Node init
mari_init(MARI_NODE, MARI_NET_ID_PATTERN_ANY, schedule, event_callback);
```

To run a Mari network on your computer, follow the wiki: https://github.com/DotBots/mari/wiki/Getting-started#running-mari-network-on-your-computer

## marilib (Python control plane)

### Install

```bash
cd marilib
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

For a Raspberry Pi edge node, `./install_marilib.sh` (inside `marilib/`) bootstraps a venv and installs the package.

### Run

```bash
mari-edge  -m mqtts://argus.paris.inria.fr:8883           # UART-attached gateway
mari-cloud -n 0x0100 -m mqtts://argus.paris.inria.fr:8883 # cloud bridge over MQTT
```

`mari-edge --help` documents all flags. The TUI shows real-time network state with an ASN-decomposed latency breakdown (`host/dl/app/ul`) and a `Q (sf)` column for per-node uplink-queue depth — see [`marilib/AGENTS.md`](marilib/AGENTS.md) for column semantics.

### Minimal Python example

```python
import time
from marilib.marilib import MarilibEdge
from marilib.serial_uart import get_default_port

def main():
    mari = MarilibEdge(lambda event, data: print(event.name, data), get_default_port())
    while True:
        for node in mari.gateway.nodes:
            mari.send_frame(dst=node.address, payload=b"A" * 3)
        time.sleep(0.25)

if __name__ == "__main__":
    main()
```

More examples in [`marilib/examples/`](marilib/examples/).

### Tests / format / build

```bash
cd marilib
hatch test                                   # pytest --doctest-modules --cov=marilib
hatch fmt --check
hatch build
```

## Branch policy

`main` is the trunk and releases are tags on it. Feature branches off `main`; PRs target `main`.

## Agent guidance

If you are an AI coding agent working in this repo, read [`AGENTS.md`](AGENTS.md) (cross-cutting), [`firmware/AGENTS.md`](firmware/AGENTS.md), and [`marilib/AGENTS.md`](marilib/AGENTS.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Publications

- Fedrecheski et al., "Mari: Connecting Large Scale Robot Swarms over BLE using TSCH with Multiple Independent Gateways", CrystalFreeIoT Workshop 2025 [Forthcoming]

## Acknowledgments

This project has received funding from the EU's Horizon Europe Framework Programme under Grant Agreement No. 101093046.
