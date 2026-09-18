#!/usr/bin/env bash
#
# build-schedules.sh - build one gateway net-core image per schedule.
#
# The gateway's schedule is a compile-time pointer (03app_gateway_net/main.c,
# `schedule_app`), so switching it means a rebuild. Nodes do not: they adopt
# whatever the beacon advertises (mari/mac.c, sync_to_gateway ->
# mr_scheduler_set_schedule), so only the gateway is ever reflashed.
#
# Building takes minutes and flashing takes seconds, so build all of them once
# and flash from the cache during a campaign:
#
#   ./build-schedules.sh                       # all four, into Output/schedules/
#   ./build-schedules.sh tiny huge             # just these
#   ./flash.sh gateway --all --net-hex Output/schedules/03app_gateway_net-tiny.hex
#
# main.c is edited in place and restored byte-for-byte from a copy taken up
# front, via an EXIT trap, so an interrupted run does not leave the source
# pointing at the wrong schedule. Uncommitted edits are preserved, not
# discarded - the bench net id lives in this very file, so that is the normal
# case rather than the exception.
#
# Env:
#   SEGGER_DIR    SES install (default: /opt/segger)
#   BUILD_CONFIG  SES config (default: Debug)

set -euo pipefail

SEGGER_DIR="${SEGGER_DIR:-/opt/segger}"
BUILD_CONFIG="${BUILD_CONFIG:-Debug}"

FW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN="$FW_DIR/app/03app_gateway_net/main.c"
OUT_DIR="$FW_DIR/Output/schedules"
BUILT_HEX="$FW_DIR/app/03app_gateway_net/Output/nrf5340-net/$BUILD_CONFIG/Exe/03app_gateway_net-nrf5340-net.hex"

SCHEDULES=("$@")
[[ ${#SCHEDULES[@]} -eq 0 ]] && SCHEDULES=(tiny medium big huge)

for s in "${SCHEDULES[@]}"; do
  case "$s" in
    tiny|medium|big|huge) ;;
    *) echo "Error: unknown schedule '$s' (tiny|medium|big|huge)" >&2; exit 2 ;;
  esac
done

# Snapshot before touching anything. The restore is byte-for-byte from this
# copy, so whatever state main.c was in - including the uncommitted net id it
# normally carries - comes back exactly.
ORIGINAL="$(mktemp)"
SCRATCH="$(mktemp)"
cp "$MAIN" "$ORIGINAL"
BUILT=()
restore() {
  rm -f "$SCRATCH"
  cp "$ORIGINAL" "$MAIN" 2>/dev/null || true
  if cmp -s "$ORIGINAL" "$MAIN"; then
    rm -f "$ORIGINAL"
  else
    echo "WARNING: could not restore $MAIN. Your original is at $ORIGINAL" >&2
  fi
  # The restore writes main.c after the images were produced from it, so
  # without this every image ends up older than the source it was built from,
  # and anything comparing the two reads a fresh build as stale. Stamping them
  # here, after the restore, is the only point at which they are last.
  if [[ ${#BUILT[@]} -gt 0 ]]; then
    touch "${BUILT[@]}"
  fi
}
trap restore EXIT

# In-place sed is not portable: BSD sed (macOS) requires an argument to -i and
# GNU sed (Linux) refuses one. Filter through a scratch file instead, which both
# accept, and write back with cat so $MAIN keeps its inode and permissions.
edit_in_place() {  # <sed-script> <file>
  sed -E "$1" "$2" > "$SCRATCH"
  cat "$SCRATCH" > "$2"
}

# sha256sum is coreutils (Linux); shasum is the perl tool macOS ships. Neither
# is present everywhere, so pick whichever exists.
sha256_list() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  else
    shasum -a 256 "$@"
  fi
}

mkdir -p "$OUT_DIR"
echo "Building ${#SCHEDULES[@]} gateway net-core image(s) into $OUT_DIR"
echo

for sched in "${SCHEDULES[@]}"; do
  echo "=== $sched ==="
  # Only the schedule_app line: a blanket substitution would also rewrite the
  # extern declaration on the line above it.
  edit_in_place "s/^(schedule_t[[:space:]]+\*schedule_app[[:space:]]*=[[:space:]]*&)schedule_[a-z]+;/\1schedule_${sched};/" "$MAIN"
  if ! grep -qE "^schedule_t[[:space:]]+\*schedule_app[[:space:]]*=[[:space:]]*&schedule_${sched};" "$MAIN"; then
    echo "Error: could not point schedule_app at schedule_${sched}" >&2
    exit 1
  fi

  SEGGER_DIR="$SEGGER_DIR" BUILD_CONFIG="$BUILD_CONFIG" make -C "$FW_DIR" gateway-net
  cp "$BUILT_HEX" "$OUT_DIR/03app_gateway_net-${sched}.hex"
  BUILT+=("$OUT_DIR/03app_gateway_net-${sched}.hex")
  echo "  -> $OUT_DIR/03app_gateway_net-${sched}.hex"
  echo
done

echo "Done."
sha256_list "$OUT_DIR"/*.hex | sed 's/^/  /'
