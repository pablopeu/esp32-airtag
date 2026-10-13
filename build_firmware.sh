#!/usr/bin/env bash
# Build the ESP32 firmware, optionally generating a fresh key set first.
#
# Steps: (1) optionally run generate_keypairs.py, (2) inject the generated
# public keys into a copy of Firmware/ESP32, (3) build it with the official
# ESP-IDF container, (4) merge bootloader + partition table + app into a single
# flashable binary. Artifacts land in ./dist/.
#
# Usage:
#   ./build_firmware.sh [options]
#
#   --keys N          generate N key pairs first (default: reuse pub_keys_c.txt
#                     if present, otherwise generate 2000)
#   --seed HEX        seed for generate_keypairs.py (reproducible key sets)
#   --keys-file PATH  build from this public-key C array instead of pub_keys_c.txt
#                     (implies --skip-keys; used by provision.py build)
#   --out-dir DIR     write the artifacts here instead of ./dist
#   --delay S         set the beacon interval in seconds (default: keep 30)
#   --skip-keys       never generate keys; fail if pub_keys_c.txt is missing
#   --native          use a locally installed ESP-IDF instead of Docker
#   --image TAG       ESP-IDF container image (default: espressif/idf:v4.4.7)
#   -h, --help        show this help

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_SRC="$REPO_DIR/Firmware/ESP32"
DIST_DIR="$REPO_DIR/dist"
KEYS_FILE="$REPO_DIR/pub_keys_c.txt"
WORK_DIR="${WORK_DIR:-$REPO_DIR/dist/.build}"
IDF_IMAGE="espressif/idf:v4.4.7"
KEYS=""
SEED=""
DELAY=""
SKIP_KEYS=0
NATIVE=0

usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keys)      KEYS="$2"; shift 2 ;;
        --seed)      SEED="$2"; shift 2 ;;
        --keys-file) KEYS_FILE="$(realpath -m "$2")"; SKIP_KEYS=1; shift 2 ;;
        --out-dir)   DIST_DIR="$(realpath -m "$2")"; shift 2 ;;
        --delay)     DELAY="$2"; shift 2 ;;
        --skip-keys) SKIP_KEYS=1; shift ;;
        --native)    NATIVE=1; shift ;;
        --image)     IDF_IMAGE="$2"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

cd "$REPO_DIR"
mkdir -p "$DIST_DIR"

# --- 1. key material --------------------------------------------------------

if [[ $SKIP_KEYS -eq 1 && ! -f "$KEYS_FILE" ]]; then
    echo "error: no public keys at $KEYS_FILE" >&2
    exit 1
fi

if [[ $SKIP_KEYS -eq 0 ]]; then
    if [[ -n "$KEYS" || ! -f "$KEYS_FILE" ]]; then
        KEY_COUNT="${KEYS:-2000}"
        echo "==> generating $KEY_COUNT key pairs"
        python3 generate_keypairs.py "$KEY_COUNT" ${SEED:+--seed "$SEED"}
    else
        echo "==> reusing existing pub_keys_c.txt (pass --keys N to regenerate)"
    fi
fi
[[ -f accessory_list.plist || $SKIP_KEYS -eq 1 ]] \
    || echo "warning: accessory_list.plist not found (needed to import tags in the server)"

# --- 2. inject the public keys into a build copy -----------------------------

echo "==> preparing build tree"
if ! rm -rf "$WORK_DIR" 2>/dev/null; then
    # A tree left behind by an older run that built as root inside the container.
    if command -v docker >/dev/null; then
        echo "    (removing a build tree owned by another user)"
        docker run --rm -v "$(dirname "$WORK_DIR"):/clean" "$IDF_IMAGE" \
            rm -rf "/clean/$(basename "$WORK_DIR")"
    else
        echo "error: cannot remove $WORK_DIR (owned by someone else); remove it yourself" >&2
        exit 1
    fi
fi
mkdir -p "$(dirname "$WORK_DIR")"
cp -r "$FIRMWARE_SRC" "$WORK_DIR"

python3 - "$WORK_DIR/main/openhaystack_main.c" "$KEYS_FILE" <<'PY'
import re
import sys

path, keys_path = sys.argv[1], sys.argv[2]
src = open(path).read()
generated = open(keys_path).read().strip()
new, count = re.subn(
    r"static uint8_t public_keys\[\]\[28\] = \{.*?\};", generated, src, count=1, flags=re.S
)
if count != 1:
    sys.exit("error: could not find the public_keys array in openhaystack_main.c")
open(path, "w").write(new)
print(f"    injected {generated.count(chr(10)) - 1} public keys from {keys_path}")
PY

if [[ -n "$DELAY" ]]; then
    echo "==> beacon interval: ${DELAY}s"
    sed -i -E "s/^#define DELAY_IN_S .*/#define DELAY_IN_S $DELAY/" "$WORK_DIR/main/openhaystack_main.c"
    grep -q "^#define DELAY_IN_S $DELAY" "$WORK_DIR/main/openhaystack_main.c" \
        || { echo "error: failed to set DELAY_IN_S" >&2; exit 1; }
fi

# --- 3. build ----------------------------------------------------------------

if [[ $NATIVE -eq 1 ]]; then
    command -v idf.py >/dev/null || { echo "error: idf.py not in PATH; install ESP-IDF or drop --native" >&2; exit 1; }
    echo "==> building with local ESP-IDF"
    ( cd "$WORK_DIR" && idf.py build )
    ESPTOOL="esptool.py"
else
    command -v docker >/dev/null || { echo "error: docker not found; use --native with ESP-IDF installed" >&2; exit 1; }
    echo "==> building in container $IDF_IMAGE (first run pulls ~1.5 GB)"
    # Build as the invoking user so the artifacts and the build tree stay ours
    # (otherwise the next run cannot clean up after root-owned files).
    DOCKER_RUN=(docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp
                -v "$WORK_DIR:/project" -w /project "$IDF_IMAGE")
    "${DOCKER_RUN[@]}" idf.py build
    ESPTOOL_CMD=("${DOCKER_RUN[@]}" esptool.py)
fi

# --- 4. collect artifacts ----------------------------------------------------

echo "==> collecting artifacts into dist/"
cp "$WORK_DIR/build/bootloader/bootloader.bin"        "$DIST_DIR/bootloader.bin"
cp "$WORK_DIR/build/partition_table/partition-table.bin" "$DIST_DIR/partition-table.bin"
cp "$WORK_DIR/build/openhaystack.bin"                 "$DIST_DIR/openhaystack.bin"

if [[ $NATIVE -eq 1 ]]; then
    "$ESPTOOL" --chip esp32 merge_bin -o "$DIST_DIR/flash-all-2mb.bin" \
        --flash_mode dio --flash_size 2MB --flash_freq 40m \
        0x1000 "$DIST_DIR/bootloader.bin" \
        0x8000 "$DIST_DIR/partition-table.bin" \
        0x10000 "$DIST_DIR/openhaystack.bin"
else
    "${ESPTOOL_CMD[@]}" --chip esp32 merge_bin -o /project/build/flash-all-2mb.bin \
        --flash_mode dio --flash_size 2MB --flash_freq 40m \
        0x1000 build/bootloader/bootloader.bin \
        0x8000 build/partition_table/partition-table.bin \
        0x10000 build/openhaystack.bin
    cp "$WORK_DIR/build/flash-all-2mb.bin" "$DIST_DIR/flash-all-2mb.bin"
fi

APP_SIZE=$(stat -c%s "$DIST_DIR/openhaystack.bin")
echo
echo "Done. App binary: $(numfmt --to=iec "$APP_SIZE") of the 1 MB app partition."
echo
echo "Flash everything in one shot (2 MB module):"
echo "  esptool.py --chip esp32 write_flash 0x0 $DIST_DIR/flash-all-2mb.bin"
echo
echo "Or flash the three parts (same as the flasher script):"
echo "  esptool.py --chip esp32 write_flash 0x1000 $DIST_DIR/bootloader.bin \\"
echo "      0x8000 $DIST_DIR/partition-table.bin 0x10000 $DIST_DIR/openhaystack.bin"
echo
echo "Keep accessory_list.plist: import it into the server to see this device."
if [[ $SKIP_KEYS -eq 1 ]]; then
    echo "Keys came from $KEYS_FILE; import the plist that sits next to it."
fi
