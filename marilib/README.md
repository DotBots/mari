# MariLib 💫 👀 🐍

MariLib is a Python library to interact with a local [Mari](https://github.com/DotBots/mari) network.
It connects to a Mari gateway via:
- UART, using MarilibEdge
- MQTT, using MarilibCloud

## Example with TUI
MariLib provides a stateful class with gateway and node information, network statistics, and a rich real-time TUI:

[mari-edge-2.webm](https://github.com/user-attachments/assets/fe50f2ba-8e67-4522-8700-69730f8e3aee)

To run with a gateway connected via UART:
```bash
# for example, using the Inria Argus MQTT broker
(.venv) $ mari-edge -m mqtts://argus.paris.inria.fr:8883
```
You can see how it works using `mari-edge --help`.

Broker credentials come from the environment, never the command line:
`MARI_MQTT_USER` / `MARI_MQTT_PASS`. `MARI_MQTT_INSECURE=1` skips the broker
certificate check (the link stays encrypted but the broker is no longer
authenticated), for a bench whose broker certificate has expired or is
self-signed.

To run with a gateway connected via MQTT:
```bash
# for example, using the Inria Argus MQTT broker
(.venv) $ mari-cloud -n 0x0100 -m mqtts://argus.paris.inria.fr:8883
```

### Reading the TUI

Most columns in the per-node table are self-explanatory (TX/RX
counts, PDR, RSSI). The two that need a key:

- **Latency | host/dl/app/ul (ms)** — total host-measured round-trip
  latency on the left of the bar, then the four-leg breakdown that
  sums (≈) to it:
  - `host` — UART round-trip plus Python parse overhead (typically
    10–30 ms at 1 Mbps; sustained higher means host-side congestion).
  - `dl` — gateway downlink-queue wait plus one radio slot.
  - `app` — node-side application turnaround (e.g. how long the
    SwarmIT network-core main loop takes between receiving the probe
    and enqueueing the response).
  - `ul` — node uplink-queue wait plus one radio slot. **This is the
    leg that grows when the node is saturating its uplink budget.**

  Wire legs come from ASN snapshots the firmware stamps into each
  probe (`gw_tx_enqueued_asn`, `node_rx_asn`,
  `node_tx_enqueued_asn`, `gw_rx_asn`) converted via the slot
  duration (1.724 ms in the shipped schedules). When the firmware
  hasn't populated the wire ASNs the row shows `total | ? / ? / ? / ?`.

- **Q (sf)** — estimated TX-queue depth at the node in **slotframe**
  units. Each joined node has exactly one uplink slot per slotframe,
  so `ul_ms / sf_duration_ms` is the number of packets queued ahead
  of the probe response when it was enqueued. Coloring: white below
  2 (healthy), yellow at 2–4 (contended), red above 4 (saturated).
  The header panel also shows a `⚠ TX queue saturated` line when any
  node crosses the red threshold.

## Setup and dependencies
To setup the environment, do:

```bash
$ python -m venv .venv
$ source .venv/bin/activate
(.venv) $ pip install -e .
```

## Minimal example
Here is a minimal example showcasing how to use MariLib:

```python
import time
from marilib.marilib import MarilibEdge
from marilib.serial_uart import get_default_port

def main():
    mari = MarilibEdge(lambda event, data: print(event.name, data), get_default_port())
    while True:
        for node in mari.gateway.nodes:
            mari.send_frame(dst=node.address, payload=b"A" * 3)
        statistics = [(f"{node.address:016X}", node.stats.received_rssi_dbm()) for node in mari.gateway.nodes]
        print(f"Network statistics: {statistics}")
        time.sleep(0.25)

if __name__ == "__main__":
    main()
```
See it in action in `examples/minimal.py`.
