# Find You — ESP32 AirTag clone with self-hosted web tracking

A modified [OpenHaystack](https://github.com/seemoo-lab/openhaystack) firmware and
tooling to build a stealth AirTag-like tag on an ESP32, plus a **self-hosted web
interface** to locate it — no Mac, no Android app, any browser.

Upstream is Positive Security's [Find You](https://positive.security/blog/find-you)
research release. This fork completes it: the keypair generator that upstream left
out as "an exercise for the reader" is implemented, the firmware is battery-friendly,
and retrieval runs on your own server. The original README is preserved as
[README.find-you.md](README.find-you.md).

> **Research tooling.** It demonstrates that Apple's unwanted-tracking protections
> can be bypassed by a device that rotates its advertised keys. Use it on devices
> you own or are explicitly authorised to track. Tracking people without their
> consent is illegal in most jurisdictions.

## How it works

```
   ESP32 tag                    iPhones nearby                  Apple
 ┌───────────┐   BLE beacon   ┌──────────────┐  encrypted   ┌────────────┐
 │ 1 beacon  │ ─────────────► │ Find My relay│ ───────────► │  reports   │
 │ per key,  │  (public key)  │  (anonymous) │   location   │  database  │
 │ 30 s      │                └──────────────┘              └─────┬──────┘
 └───────────┘                                                    │ hashed keys
       ▲                                                          ▼
       │ flashed public keys                        ┌──────────────────────────┐
       │                                             │  your server (this repo) │
 ┌─────┴─────────────┐   accessory_list.plist       │  fetch → decrypt → map   │
 │ generate_keypairs │ ───────────────────────────► │  private keys stay here  │
 └───────────────────┘   (private keys)             └──────────────────────────┘
```

Each key is advertised exactly once — one beacon, then the next key. Because the
public key never repeats within a full rotation, the tag is not flagged as
"following" anyone, which is what Apple's anti-stalking logic keys on. The phone
uploads an encrypted location report per beacon; only the private key (yours,
on your server) can decrypt it.

The rotation is what the [blog post](https://positive.security/blog/find-you)
calls the core trick: 2000 keys at one beacon every 30 s repeat after ~16.7 hours.

## What is in this repository

| Path | What it is |
|---|---|
| `generate_keypairs.py` | Generates the key set: public keys as a C array + private keys as an OpenHaystack plist |
| `provision.py` | Runs several tags from one key pool with disjoint slices, and verifies that none collide |
| `build_firmware.sh` | One command: generate keys → inject → build with ESP-IDF → single flashable binary in `dist/` |
| `Firmware/ESP32/` | ESP-IDF project; advertises one preloaded key per boot, then deep-sleeps |
| `server/` | Self-hosted web tracker (Docker Compose: app + anisette + Caddy/HTTPS) — see [server/README.md](server/README.md) |
| `OpenHaystack/` | Upstream macOS app (alternative retrieval client, needs a Mac + Xcode) |
| `Firmware/Microbit_v1/`, `Firmware/Linux_HCI/` | Upstream firmwares for other hardware |
| `README.Reproducibility.md`, `CVE-2020-9986/`, `Resources/` | Upstream research artifacts |
| `README.find-you.md` | The historical upstream README |

## Requirements

- **Keys + build:** Python 3 (stdlib only) and Docker (the ESP-IDF toolchain runs in
  a container; no local ESP-IDF install needed).
- **Flashing:** `esptool` — `pip install esptool`, or use `Firmware/ESP32/flash_esp32.sh`
  which sets up its own virtualenv.
- **Server:** Docker + Compose v2, an Apple ID, and a hostname if you want HTTPS.
- **Hardware:** an ESP32 module (2 MB flash or more; a bare WROOM/WROVER works).

---

## 1. Generate the key set

```bash
python3 generate_keypairs.py 2000
```

This writes two files in the repository root:

| File | Contents | Where it goes |
|---|---|---|
| `pub_keys_c.txt` | 2000 public keys (28-byte X coordinates) as a C array | Injected into the firmware |
| `accessory_list.plist` | The matching 2000 private keys | Imported into the server (or the macOS app) |

Options:

```bash
python3 generate_keypairs.py 5000              # more keys = longer rotation
python3 generate_keypairs.py 2000 --seed AABB… # reproducible key set (demo/testing)
```

**Key count vs. rotation time.** The tag sends one beacon per key, then waits
`DELAY_IN_S` (default 30 s). A full cycle therefore takes `count × 30 s`:
1000 keys ≈ 8.3 h, 2000 ≈ 16.7 h, 5000 ≈ 41.7 h. More keys means the same public
key reappears less often (less chance of being clustered as a follower), at the
cost of flash space (28 bytes per key) and a slower per-key location update.

**`accessory_list.plist` contains your private keys.** It is the only thing that
can decrypt the tag's locations. Never commit it (it is gitignored here), never
share it, and back it up: lose it and the tag's history is unreadable forever.

> **More than one tag?** Generating a set per device and keeping them apart by hand
> is how collisions happen. Use [`provision.py`](#running-several-tags) instead: one
> key pool, a disjoint slice per device, and a `verify` that proves it.

> **Demo seed.** Release v1.0.0 ships a binary and a matching plist built from the
> public seed `66696e642d796f752d64656d6f2d7631` (see
> [the demo binary](#no-build-at-all-the-demo-binary)). Anyone can derive those
> private keys — demo only, never for a real deployment.

---

## 2. Build the firmware

### One command (recommended)

```bash
./build_firmware.sh --keys 2000        # or: --skip-keys to reuse pub_keys_c.txt
```

The script generates the keys if needed, injects them into a copy of the firmware,
builds inside the official `espressif/idf:v4.4.7` container (first run pulls ~1.5 GB)
and merges everything into `dist/`:

```
dist/openhaystack.bin          the app (flash at 0x10000)
dist/bootloader.bin            (0x1000)
dist/partition-table.bin       (0x8000)
dist/flash-all-2mb.bin         all three merged (flash at 0x0)
```

Useful flags: `--delay 60` (beacon interval), `--seed HEX`, `--native` (use a local
ESP-IDF instead of Docker), `--image TAG` (different ESP-IDF container),
`--keys-file PATH` and `--out-dir DIR` (build from, and write to, somewhere else —
this is what `provision.py build` uses).

### Manual build

1. Copy the array from `pub_keys_c.txt` over the `public_keys` definition in
   `Firmware/ESP32/main/openhaystack_main.c` (around line 46).
2. Optionally change `#define DELAY_IN_S 30`.
3. Build with ESP-IDF ≥ v4.4: `idf.py build` inside `Firmware/ESP32/`.

The app partition is 1 MB; each key costs 28 bytes (2000 keys ≈ 55 KB), so a
2000-key build lands at ~752 KB — 73% of the partition. If you hit "binary size
exceeds partition" with very large key sets, reduce the key count or enlarge
`factory` in `Firmware/ESP32/partitions.csv`.

### No build at all: the demo binary

Release [v1.0.0](https://github.com/pablopeu/esp32-airtag/releases/tag/v1.0.0)
ships a ready-to-flash image built from the public demo seed, so you can test the
whole pipeline before touching the toolchain:

```
openhaystack-esp32-demo-flash-all-2mb.bin    flash at 0x0
accessory_list_demo.plist                    import into the server
```

**Read this before flashing the demo image.** Every ESP32 flashed with it advertises
the *same* 2000 keys — and in Find My a tag's identity *is* its key; there is no
per-device identifier in a location report, by design. So two demo tags are not two
similar tags, they are **one tag**: your server fetches and decrypts the reports of
all of them, and their locations merge into a single trail that cannot be taken
apart. If someone else runs the demo in another city, your dashboard will happily
show their position as your tag's latest, interleaved with yours in the history.
(The plist is public too, so a third party can watch your demo tag as well.)

Treat it strictly as a smoke test: flash it, confirm the pipeline works end to end,
then generate your own keys. For anything real,
`python3 generate_keypairs.py 2000` — its default random seed gives you a key set
that nobody else has. The same applies to `--seed`: it exists for reproducible
tests, and two people using the same seed get the same collision.

### Battery behaviour

Each wake-up sends exactly one beacon (~100 ms of radio), stops advertising and
enters `esp_deep_sleep` (~10 µA on a bare module) for the beacon interval. The
rotation index is kept in RTC memory so the sequence continues across sleep cycles.

Expect weeks to months on a LiPo cell (e.g. ~300 µA average for a 30 s interval,
including the boot overhead) — but note that a typical **development board adds its
own quiescent drain** (USB-serial chip, LDO, power LED: often 100–500 µA), which can
dominate the budget. For a long-lived build, use a bare module or remove those parts.

Avoid rebuilding with a *different* key count and re-flashing without erasing: the
firmware detects the mismatch and restarts the rotation from key 0, so it is safe,
but the old rotation position is lost.

---

## 3. Flash the ESP32

```bash
# Single file (2 MB module, erases nothing else):
esptool.py --chip esp32 write_flash 0x0 dist/flash-all-2mb.bin

# Or the three parts, same layout the repo's flasher script uses:
esptool.py --chip esp32 write_flash \
    0x1000 dist/bootloader.bin \
    0x8000 dist/partition-table.bin \
    0x10000 dist/openhaystack.bin
```

`Firmware/ESP32/flash_esp32.sh -p /dev/ttyUSB0` does the three-part flash for you
(create the venv with esptool on first run). Watch the serial log at 115200 baud:
each boot prints the key index, the random device address and the deep-sleep entry.

An ESP32 alone is enough — no phone, no pairing, no account on the device.

---

## 4. Run the tracking server

Full details, including options and troubleshooting, are in
[`server/README.md`](server/README.md). The short path:

```bash
cd server
cp .env.example .env
$EDITOR .env            # DOMAIN, ADMIN_USER, ADMIN_PASSWORD
docker compose up -d --build
```

That starts three containers: the tracker app, `anisette-server` (it supplies the
Apple attestation headers, which is why no Mac is required) and Caddy, which obtains
a Let's Encrypt certificate for `DOMAIN` automatically.

### On an Oracle Cloud free VM

1. **Firewall, both layers** (Oracle blocks 80/443 by default):
   - VCN → Security List → ingress rules for TCP 80 and 443.
   - On the instance:
     ```bash
     sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
     sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
     sudo netfilter-persistent save
     ```
2. **DNS**: point a subdomain at the VM, e.g. in your `example.com` zone:
   `tag.example.com.  CNAME  <vm-public-ip-or-hostname>.`
3. `docker compose up -d --build`, then open `https://tag.example.com`.

For a LAN-only deployment, drop the `caddy` service, publish the app port directly
and set `HTTPS_ONLY=0`.

### First run

1. **Log in** with `ADMIN_USER` / `ADMIN_PASSWORD`.
2. **Connect the Apple ID** in the *Apple account* card. The password is used once to
   obtain tokens and is never stored; only the resulting session is written to the
   data volume. Enter the 2FA code when prompted (trusted device or SMS). Use an
   Apple ID you can afford to lose — Apple's Find My API is undocumented and
   accounts have been locked for unusual API use.
3. **Import the tag**: *Add tags* → upload `accessory_list.plist` → keep
   **Merge all keys into one tag** checked (one physical ESP32 = one tag with all its
   keys) → Import. You can also paste individual keys (hex or base64, one per line)
   for tags that use a single fixed key.
4. **Wait for a location.** The map fills as soon as an iPhone passes near the tag
   and the next poll runs (default every 15 min; use **Refresh** to poll now).

### Using it

- **Dashboard**: every tag with its key count, last position and age; add, delete,
  refresh.
- **Tag page**: Leaflet map with the latest position, the trail for the selected
  window (6 h – 30 days) and the report table (click a row to zoom).
- Reports are stored locally in SQLite (`RETENTION_DAYS`, default 30). Only hashed
  public keys ever leave your server.

### Operating it

```bash
docker compose logs -f tracker                          # poll results, errors
docker compose pull && docker compose up -d --build     # update
docker compose down                                     # stop (volumes persist)
```

Back up the `tracker-data` volume: it holds the database, the Apple session and the
cookie secret. If you lose it you must re-import the plist and log into Apple again.

Set `ADMIN_PASSWORD_HASH` instead of a plaintext password with:

```bash
docker compose exec tracker python -c \
  "from app.security import hash_password; print(hash_password('your-password'))"
```

---

## Running several tags

Everything above describes one device. As soon as you run two, a new failure mode
appears: to Find My a tag **is** its keys, so two devices that share any key are one
identity whose locations merge with no way to separate them afterwards (the demo
binary in section 2 is that case on purpose).

`provision.py` rules it out by construction: it generates one large key pool and
gives each device its own **disjoint slice** of it.

```bash
python3 provision.py pool --keys 10000          # once, for the whole fleet
python3 provision.py device garage --keys 2000  # allocate a slice per device
python3 provision.py build garage               # keys → firmware, via build_firmware.sh
python3 provision.py device car --keys 3000     # the next free slice
python3 provision.py build car

python3 provision.py status                     # pool and devices at a glance
python3 provision.py verify                     # proves there is no collision
```

Each `device` command creates `devices/<name>/`, all of it gitignored:

| File | What it is |
|---|---|
| `pub_keys_c.txt` | that device's slice, as the firmware C array |
| `accessory_list.plist` | that device's slice, for the server import |
| `manifest.json` | slice range, key count, fingerprint, dates |
| `dist/` | the firmware, once you run `build` |

The allocation is recorded in `devices/registry.json`, so slices are handed out from
the first free offset (`--offset` to place one explicitly) and never overlap. Each
device keeps its own rotation — a cycle is `count × beacon interval` — so differently
sized slices just rotate at their own rate, independently.

**Sizes.** The pool only needs to be at least the sum of the devices' key counts:
10000 keys cover five 2000-key devices. Keys cost 28 bytes of flash each, and the app
partition is 1 MB, so a slice can be large — a ~700 KB base firmware leaves room for
roughly 11000 keys — but remember that a longer rotation is also a slower per-key
location update, and that the pool has to cover every device at once.

**Importing.** Flash the device as in section 3, then in the server upload
`devices/<name>/accessory_list.plist`, type the device name in the *Name* field and
keep **Merge all keys into one tag** checked. One device becomes one tag, and the
tag's name is what you typed.

### What `verify` proves

```bash
python3 provision.py verify          # derives every public key (~5 s for 5000)
python3 provision.py verify --fast   # samples 20 keys per device instead
```

For every device it checks that the private keys in the plist really derive the
public keys in the C array, that the file still matches the fingerprint recorded at
provisioning time, that no two devices share a key, and — once a firmware exists —
that the firmware carries that device's whole key set and none of another's. That
last check parses the ESP32 app image and rebuilds the memory layout rather than
searching the raw `.bin`: a key can straddle the boundary between two image
segments, in which case it is contiguous in DRAM but never contiguous in the file.

Run it after provisioning a device, and after any `cp`, restore or rebuild that
moves keys, plists or binaries around. It exits non-zero and names the device when
something is wrong, which is the point: a collision is silent otherwise — you find
out when a tag shows up in a city you have never visited.

### The pool is a master key

Holding one pool means one secret unlocks every device provisioned from it, which is
a real trade for the collision-free guarantee. So: back it up with the per-device
plists, keep it off the server, and note that you can **delete `keys/` once every
device is provisioned** — the per-device plists are enough to track them, and the
pool is only needed to allocate the next slice.

Never commit `keys/`, `devices/` or any `accessory_list*.plist`: they hold the
private keys, and a leaked plist means anyone can follow that tag. Both directories
are already in `.gitignore`; the only thing that ever leaves your machine is the
firmware, whose public keys the device broadcasts over BLE anyway.

---

## Alternative: the macOS app

`OpenHaystack/` still contains the upstream macOS retrieval application, which this
fork's `accessory_list.plist` is also compatible with. It needs a Mac with Xcode;
build and run it, then *Import accessories* and select the plist. It fetches and
decrypts exactly like the server does. For a headless setup (a VPS, a NAS, a Pi),
use `server/` instead.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No location for hours | Normal if no Apple device passed nearby. A tag sitting at home updates whenever a phone comes close (delivery people, neighbours, passers-by). |
| Location is old but plausible | Reports only refresh while the tag is advertising (it sleeps between beacons) and someone relays it. Increase `POLL_MINUTES` only if you need fresher data — Apple rate-limits. |
| `refresh failed: no_apple_account` | Connect the Apple ID in the UI. |
| Apple login fails or loops on 2FA | Wrong password, an Apple ID without Find My, or a flagged account. Try another Apple ID. |
| `apple_state_require_2fa` in logs | The session expired; log in again and complete 2FA. |
| Tag never appears, no key index in serial log | Firmware not flashed correctly (check the offsets) or the plist was imported into a different server. |
| `binary size exceeds partition` | Too many keys for the 1 MB app partition: lower the key count or resize `factory` in `partitions.csv`. |
| Build fails on a newer ESP-IDF | Use the container (`./build_firmware.sh`), which pins IDF v4.4.7. |
| `verify`: two devices share keys | The pool was regenerated, or a file was copied between device directories. Re-provision the affected device with `--offset` inside its own free range, or rebuild the pool and every device. |
| `verify`: keys missing from the firmware | The firmware in `devices/<name>/dist` was not built from that device's keys. Run `provision.py build <name>`, and import the plist that sits next to it. |
| `cannot remove …/.build` after a build | An older run built as root in the container. Current versions build as your user; clear the old tree with `sudo rm -rf dist/.build` once. |

## Credits and licence

- Original research and release: [Positive Security](https://positive.security/blog/find-you);
  base framework: [OpenHaystack](https://github.com/seemoo-lab/openhaystack) (SEEMOO, TU Darmstadt).
- Key generation, battery-optimised rotation, build tooling and the web server: this fork.
- Licence: AGPL-3.0-only, as upstream — see [LICENSE](LICENSE).
