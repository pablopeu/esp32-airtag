#!/usr/bin/env python3
"""Provision several ESP32 tags from one key pool, with disjoint key slices.

`generate_keypairs.py` produces one key set for a single device. As soon as you
run more than one tag, no two devices may share a key: to Find My a tag *is* its
keys, so overlapping sets are a single identity whose locations merge with no way
to separate them afterwards.

This script allocates each device its own slice of one larger pool, so a
collision is impossible by construction:

  keys/pool/                 the pool: every private key of the fleet
      pub_keys_c.txt             all public keys, generation order
      accessory_list.plist       all private keys, same order
  devices/<name>/            one directory per device
      pub_keys_c.txt             that device's slice, as the firmware C array
      accessory_list.plist       that device's slice, for the server import
      manifest.json              slice, count, fingerprint, dates
      dist/                      the built firmware, after `build`
  devices/registry.json      which slice ranges are taken

`verify` then proves it after the fact: every device's private keys must match
its own public keys, no two devices may share a key, and if a firmware binary has
been built it must contain that device's keys and none of another's.

Everything under keys/ and devices/ holds private keys or fleet layout, and both
are gitignored. Nothing here leaves the machine except the firmware, which
contains public keys only -- the ones the device broadcasts over BLE anyway.

Usage:
  provision.py pool  --keys 10000 [--seed HEX] [--force]
  provision.py device NAME [--keys 2000] [--offset N]
  provision.py build NAME [--delay S]
  provision.py verify [--fast]
  provision.py status
"""

import argparse
import hashlib
import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import generate_keypairs as gk  # noqa: E402  (public_x_bytes, write_c_array, ...)

POOL_DIR = REPO / "keys" / "pool"
DEVICES_DIR = REPO / "devices"
REGISTRY = DEVICES_DIR / "registry.json"
DEFAULT_KEYS = 2000
NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def check_name(name: str) -> str:
    """A device name becomes a directory name; keep it boring."""
    if not name or not set(name) <= NAME_CHARS or name in (".", ".."):
        sys.exit(f"invalid device name {name!r}: use letters, digits, '.', '_' or '-'")
    return name


def read_c_array(path: Path) -> list[bytes]:
    """Parse the `{0x.., 0x.., ...},` lines of a pub_keys_c.txt file."""
    keys = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        body = line.strip("{} \t,")
        if not body:
            continue
        values = [int(tok.strip(), 16) for tok in body.split(",") if tok.strip()]
        if len(values) != 28:
            sys.exit(f"{rel(path)}: expected 28 bytes per key, got {len(values)}")
        keys.append(bytes(values))
    if not keys:
        sys.exit(f"{rel(path)}: no public keys found")
    return keys


def read_plist_keys(path: Path) -> list[bytes]:
    """Private keys of a plist, in file order."""
    with open(path, "rb") as f:
        plist = plistlib.load(f)
    if isinstance(plist, dict):
        plist = plist.get("accessories", [])
    keys = []
    for acc in plist:
        if isinstance(acc, dict) and "privateKey" in acc:
            keys.append(bytes(acc["privateKey"]))
    if not keys:
        sys.exit(f"{rel(path)}: no accessories with a privateKey")
    return keys


def fingerprint(public_keys) -> str:
    """Short id of a key *set* (order-independent), for eyeballing and logs."""
    digest = hashlib.sha256()
    for key in sorted(public_keys):
        digest.update(key)
    return digest.hexdigest()[:16]


def load_registry() -> dict:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text())
    return {"pool": None, "devices": {}}


def save_registry(reg: dict) -> None:
    DEVICES_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")


def device_dir(name: str) -> Path:
    return DEVICES_DIR / name


def require_pool() -> tuple[list[bytes], list[bytes]]:
    for filename in ("pub_keys_c.txt", "accessory_list.plist"):
        if not (POOL_DIR / filename).exists():
            sys.exit(f"no key pool: {rel(POOL_DIR / filename)} is missing. "
                     f"Run `provision.py pool --keys N` first.")
    priv, pub = read_plist_keys(POOL_DIR / "accessory_list.plist"), read_c_array(
        POOL_DIR / "pub_keys_c.txt")
    if len(priv) != len(pub):
        sys.exit(f"pool is inconsistent: {len(priv)} private vs {len(pub)} public keys")
    return priv, pub


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_pool(args) -> None:
    if (POOL_DIR / "pub_keys_c.txt").exists() and not args.force:
        sys.exit(f"a pool already exists at {rel(POOL_DIR)}; regenerate with --force only "
                 f"if no device was provisioned from it (the slices would change)")

    reg = load_registry()
    if reg["devices"] and args.force:
        sys.exit("refusing to regenerate the pool while devices exist: their slices "
                 "would no longer match. Delete devices/registry.json to override.")

    POOL_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(REPO / "generate_keypairs.py"), str(args.keys)]
    if args.seed:
        cmd += ["--seed", args.seed]
    print(f"==> generating a pool of {args.keys} keys in {rel(POOL_DIR)}")
    subprocess.run(cmd, cwd=POOL_DIR, check=True)

    # The seed is deliberately not recorded: it regenerates every private key in
    # the fleet, which is strictly more powerful than the pool itself.
    reg["pool"] = {"size": args.keys, "created": now()}
    save_registry(reg)
    print(f"    pool ready: {args.keys} keys. Keep {rel(POOL_DIR)} private.")


def cmd_device(args) -> None:
    name = check_name(args.name)
    if device_dir(name).exists() and not args.force:
        sys.exit(f"device {name} already exists at {rel(device_dir(name))}")
    if args.keys < 1:
        sys.exit("--keys must be >= 1")

    priv_pool, pub_pool = require_pool()
    reg = load_registry()
    pool_size = len(pub_pool)

    if args.offset is not None:
        offset = args.offset
    else:
        offset = max((d["offset"] + d["count"] for d in reg["devices"].values()), default=0)

    if offset < 0 or offset + args.keys > pool_size:
        sys.exit(f"slice {offset}:{offset + args.keys} does not fit in the pool of "
                 f"{pool_size} keys (make the pool bigger, or lower --keys)")

    for other, entry in sorted(reg["devices"].items()):
        if other == name:  # re-provisioning this device with --force
            continue
        start, end = entry["offset"], entry["offset"] + entry["count"]
        if offset < end and start < offset + args.keys:
            sys.exit(f"slice {offset}:{offset + args.keys} overlaps device {other} "
                     f"({start}:{end}); pick another --offset")

    priv = priv_pool[offset:offset + args.keys]
    pub = pub_pool[offset:offset + args.keys]

    out = device_dir(name)
    out.mkdir(parents=True, exist_ok=True)
    gk.write_c_array(pub, out / "pub_keys_c.txt")

    accessories = []
    for i, (priv_key, pub_key) in enumerate(zip(priv, pub), 1):
        accessory = gk.build_accessory(i, priv_key, pub_key)
        accessory["name"] = f"{name} {i:04d}"
        accessories.append(accessory)
    with open(out / "accessory_list.plist", "wb") as f:
        plistlib.dump(accessories, f)

    manifest = {
        "name": name,
        "offset": offset,
        "count": args.keys,
        "pool_size": pool_size,
        "fingerprint": fingerprint(pub),
        "created": now(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    reg["devices"][name] = manifest
    save_registry(reg)

    if args.force and (out / "dist").exists():
        print(f"    note: {rel(out / 'dist')} holds a firmware built from the previous "
              f"keys and no longer matches; run `provision.py build {name}` again")
    print(f"==> device {name}: keys {offset}..{offset + args.keys - 1} "
          f"of the {pool_size}-key pool")
    print(f"    {rel(out / 'pub_keys_c.txt')}       to build the firmware from")
    print(f"    {rel(out / 'accessory_list.plist')} import into the server as one tag")
    print(f"    fingerprint {manifest['fingerprint']}")
    print(f"\n    next: provision.py build {name}")


def cmd_build(args) -> None:
    name = check_name(args.name)
    out = device_dir(name)
    keys_file = out / "pub_keys_c.txt"
    if not keys_file.exists():
        sys.exit(f"unknown device {name}: {rel(keys_file)} is missing")

    cmd = [str(REPO / "build_firmware.sh"),
           "--keys-file", str(keys_file),
           "--out-dir", str(out / "dist")]
    if args.delay:
        cmd += ["--delay", str(args.delay)]
    print(f"==> building {name} ({len(read_c_array(keys_file))} keys)")
    subprocess.run(cmd, cwd=REPO, check=True)

    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["built"] = now()
    manifest["delay"] = args.delay or 30
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\n    flash: esptool.py --chip esp32 write_flash 0x0 "
          f"{rel(out / 'dist' / 'flash-all-2mb.bin')}")
    print(f"    then import {rel(out / 'accessory_list.plist')} into the server")


APP_IMAGE_MAGIC = 0xE9
APP_HEADER_SIZE = 24  # 8-byte image header + 16-byte extended header (ESP32)
MERGED_APP_OFFSET = 0x10000  # flash-all-2mb.bin places the app image here


def app_image(dist: Path):
    """The app image bytes, from either artifact the build produces."""
    app = dist / "openhaystack.bin"
    if app.exists():
        return app.read_bytes()
    merged = dist / "flash-all-2mb.bin"
    if merged.exists():
        blob = merged.read_bytes()
        if len(blob) > MERGED_APP_OFFSET and blob[MERGED_APP_OFFSET] == APP_IMAGE_MAGIC:
            return blob[MERGED_APP_OFFSET:]
    return None


def address_blocks(image: bytes) -> list[bytes]:
    """The image as the device sees it after loading.

    A .bin is *not* a flat dump of memory: esptool writes each loaded segment at
    its load address but in file order, so unrelated segments can sit between two
    halves of one object. Here the segments are put back in address order and
    joined where their addresses are contiguous, which is the layout the
    bootloader reproduces in DRAM. A key that straddles a segment boundary is
    therefore contiguous here even though it never appears contiguously in the
    file -- searching the raw .bin for it reports a false problem.
    """
    if not image or image[0] != APP_IMAGE_MAGIC:
        sys.exit("not an ESP32 app image")
    pos, segments = APP_HEADER_SIZE, []
    for _ in range(image[1]):
        address = int.from_bytes(image[pos:pos + 4], "little")
        length = int.from_bytes(image[pos + 4:pos + 8], "little")
        pos += 8
        data = image[pos:pos + length]
        if len(data) != length:
            sys.exit("truncated app image; is this an ESP32 image?")
        segments.append((address, data))
        pos += length

    blocks, current, end = [], b"", None
    for address, data in sorted(segments):
        if current and address == end:
            current += data
        else:
            if current:
                blocks.append(current)
            current = data
        end = address + len(data)
    if current:
        blocks.append(current)
    return blocks


def missing_from_blocks(keys, blocks) -> set[int]:
    """Indices of `keys` that are not present in the device's memory image."""
    # Usual case: the whole array is contiguous, so one search settles it.
    if b"".join(keys) in b"".join(blocks):
        return set()
    return {i for i, key in enumerate(keys)
            if not any(key in block for block in blocks)}


def _check_device(name: str, entry: dict, fast: bool, blocks) -> list[str]:
    """Everything that can be wrong with one device, as a list of problems."""
    problems = []
    out = device_dir(name)

    try:
        pub = read_c_array(out / "pub_keys_c.txt")
        priv = read_plist_keys(out / "accessory_list.plist")
    except (SystemExit, OSError) as exc:
        return [f"{name}: {exc}"]

    if len(pub) != entry["count"] or len(priv) != entry["count"]:
        problems.append(f"{name}: manifest says {entry['count']} keys, files have "
                        f"{len(pub)} public / {len(priv)} private")
        return problems

    if fingerprint(pub) != entry["fingerprint"]:
        problems.append(f"{name}: public keys no longer match the recorded fingerprint "
                        f"({fingerprint(pub)} != {entry['fingerprint']})")

    # The private keys must be the ones that produce these public keys. Deriving
    # all of them in pure Python is the slow part, so --fast samples instead.
    if fast:
        step = max(1, len(priv) // 20)
        indices = list(range(0, len(priv), step))
        label = f"{len(indices)} sampled"
    else:
        indices = list(range(len(priv)))
        label = f"all {len(priv)}"
    mismatched = [i for i in indices if gk.public_x_bytes(priv[i]) != pub[i]]
    if mismatched:
        problems.append(f"{name}: {len(mismatched)} of {label} private keys do not derive "
                        f"their public key (first at index {mismatched[0]}); the "
                        f"plist and the C array are not the same key set")

    # A built firmware must carry this device's whole key set, in the layout the
    # device ends up with in DRAM.
    if blocks is not None:
        missing = missing_from_blocks(pub, blocks)
        if missing:
            problems.append(f"{name}: {len(missing)} of {len(pub)} keys are missing from "
                            f"{rel(out / 'dist')} (first at index {min(missing)}); this "
                            f"firmware was not built from this device's keys")
    return problems


def cmd_verify(args) -> None:
    reg = load_registry()
    if not reg["devices"]:
        sys.exit("no devices provisioned yet")

    problems = []
    keylists, blocks_by_device = {}, {}
    for name in sorted(reg["devices"]):
        try:
            keylists[name] = read_c_array(device_dir(name) / "pub_keys_c.txt")
        except SystemExit:
            keylists[name] = []
        image = app_image(device_dir(name) / "dist")
        blocks_by_device[name] = address_blocks(image) if image else None

    for name, entry in sorted(reg["devices"].items()):
        print(f"  {name:<16} {entry['count']} keys  fingerprint {entry['fingerprint']}"
              f"{'  [firmware built]' if blocks_by_device.get(name) else ''}")
        problems += _check_device(name, entry, args.fast, blocks_by_device.get(name))

    # No two devices may share a key, and no firmware may carry another's.
    print("  checking overlaps")
    names = [n for n in sorted(keylists) if keylists[n]]
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            shared = set(keylists[first]) & set(keylists[second])
            if shared:
                problems.append(f"{first} and {second} share {len(shared)} keys; they are "
                                f"the same identity to Find My and their locations will merge")

    for name in names:
        blocks = blocks_by_device.get(name)
        if blocks is None:
            continue
        for other in names:
            if other == name:
                continue
            foreign = len(keylists[other]) - len(missing_from_blocks(keylists[other], blocks))
            if foreign:
                problems.append(f"the firmware in {rel(device_dir(name) / 'dist')} carries "
                                f"{foreign} keys belonging to {other}; it was built from the "
                                f"wrong key set or copied from the wrong device")

    if problems:
        print()
        for problem in problems:
            print(f"  FAIL  {problem}")
        sys.exit(f"\n{len(problems)} problem(s) found")

    built = sum(1 for n in names if blocks_by_device.get(n))
    print(f"\n  OK  {len(names)} device(s), no shared keys"
          f"{f', {built} firmware image(s) match their key sets' if built else ''}")


def cmd_status(args) -> None:
    reg = load_registry()
    pool = reg.get("pool")
    if pool:
        print(f"pool: {pool['size']} keys, created {pool['created']} ({rel(POOL_DIR)})")
    else:
        print(f"pool: none ({rel(POOL_DIR)})")
    if not reg["devices"]:
        print("devices: none")
        return
    used = max(d["offset"] + d["count"] for d in reg["devices"].values())
    print(f"devices: {len(reg['devices'])}, {used} keys allocated")
    print(f"  {'name':<16} {'slice':<14} {'keys':>5}  {'built':<11} fingerprint")
    for name, entry in sorted(reg["devices"].items(), key=lambda kv: kv[1]["offset"]):
        built = "yes" if (device_dir(name) / "dist" / "flash-all-2mb.bin").exists() else "no"
        print(f"  {name:<16} {entry['offset']:>6}:{entry['count']:<6} {entry['count']:>5}  "
              f"{built:<11} {entry['fingerprint']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision several ESP32 tags from one key pool, with disjoint slices.",
        epilog="Everything under keys/ and devices/ holds private keys; both are gitignored.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pool = sub.add_parser("pool", help="generate the shared key pool")
    p_pool.add_argument("--keys", type=int, default=10000,
                        help="pool size, i.e. the sum of all the devices' key counts "
                             "(default: 10000)")
    p_pool.add_argument("--seed", metavar="HEX", help="hex seed (reproducible pool; not stored)")
    p_pool.add_argument("--force", action="store_true", help="regenerate an existing pool")
    p_pool.set_defaults(func=cmd_pool)

    p_dev = sub.add_parser("device", help="allocate a slice and provision one device")
    p_dev.add_argument("name", help="device name, e.g. garage")
    p_dev.add_argument("--keys", type=int, default=DEFAULT_KEYS,
                       help=f"keys for this device (default: {DEFAULT_KEYS})")
    p_dev.add_argument("--offset", type=int, help="explicit pool offset (default: next free)")
    p_dev.add_argument("--force", action="store_true", help="overwrite an existing device")
    p_dev.set_defaults(func=cmd_device)

    p_build = sub.add_parser("build", help="build one device's firmware")
    p_build.add_argument("name")
    p_build.add_argument("--delay", type=int, help="beacon interval in seconds (default: 30)")
    p_build.set_defaults(func=cmd_build)

    p_verify = sub.add_parser("verify", help="check slices, files and firmware for collisions")
    p_verify.add_argument("--fast", action="store_true",
                          help="sample private keys instead of deriving all of them")
    p_verify.set_defaults(func=cmd_verify)

    sub.add_parser("status", help="show the pool and the provisioned devices").set_defaults(
        func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
