#!/usr/bin/env python3
"""Pre-generate EC key pairs for the Find You firmware and retrieval app.

Generates N key pairs on the curve used by Apple's offline finding
(secp224r1 / NID P-224) and writes two files:

  pub_keys_c.txt        C array of the 28-byte public keys (the X coordinate
                        of the compressed public key) to paste over the
                        `public_keys` definition in
                        Firmware/ESP32/main/openhaystack_main.c
  accessory_list.plist  OpenHaystack-compatible property list with the
                        private keys (28-byte big-endian scalars), to import
                        in the retrieval application

Usage:
  python3 generate_keypairs.py [count] [--seed HEX]

Options:
  count   number of key pairs to generate (default 2000; with the default
          30 s beacon delay one full rotation takes count * 30 s, e.g.
          ~17 h for 2000 keys)
  --seed  hex seed for deterministic key derivation (HMAC-DRBG); regenerating
          with the same seed yields the same keys

The private key format matches what the app feeds to BN_bin2bn() and what
BoringSSL.m derives the public key from: a 28-byte big-endian scalar in
[1, n-1]. The advertised key format matches EC_POINT_point2oct() with
POINT_CONVERSION_COMPRESSED minus the 0x02/0x03 prefix: the 28-byte X
coordinate.
"""

import argparse
import hashlib
import hmac
import plistlib
import secrets
import sys
import time

# ---------------------------------------------------------------------------
# secp224r1 (NIST P-224) in pure Python — no third-party dependencies.
# ---------------------------------------------------------------------------

P = 2**224 - 2**96 + 1
A = P - 3
B = 0xB4050A850C04B3ABF54132565044B0B7D7BFD8BA270B39432355FFB4
GX = 0xB70E0CBD6BB4BF7F321390B94A03C1D356C21122343280D6115C1D21
GY = 0xBD376388B5F723FB4C22DFE6CD4375A05A07476444D5819985007E34
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFF16A2E0B8F03E13DD29455C5C2A3D


def _inv(x: int) -> int:
    """Modular inverse mod P (P is prime, Fermat)."""
    return pow(x, P - 2, P)


def _double(pt):
    """Double a point in Jacobian coordinates (x, y, z)."""
    x, y, z = pt
    if y == 0:
        return (1, 1, 0)
    ysq = (y * y) % P
    s = (4 * x * ysq) % P
    m = (3 * x * x + A * z * z * z * z) % P
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * ysq * ysq) % P
    z3 = (2 * y * z) % P
    return (x3, y3, z3)


def _add(pt1, pt2):
    """Add two Jacobian points."""
    x1, y1, z1 = pt1
    x2, y2, z2 = pt2
    if z1 == 0:
        return pt2
    if z2 == 0:
        return pt1
    z1sq = (z1 * z1) % P
    z2sq = (z2 * z2) % P
    u1 = (x1 * z2sq) % P
    u2 = (x2 * z1sq) % P
    s1 = (y1 * z2sq * z2) % P
    s2 = (y2 * z1sq * z1) % P
    if u1 == u2:
        return (1, 1, 0) if s1 != s2 else _double(pt1)
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    hsq = (h * h) % P
    hcu = (hsq * h) % P
    u1hsq = (u1 * hsq) % P
    x3 = (r * r - hcu - 2 * u1hsq) % P
    y3 = (r * (u1hsq - x3) - s1 * hcu) % P
    z3 = (h * z1 * z2) % P
    return (x3, y3, z3)


def _scalar_mult(k: int, affine_pt):
    """k * point via double-and-add on the Jacobian ladder."""
    result = (1, 1, 0)
    addend = (affine_pt[0] % P, affine_pt[1] % P, 1)
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _double(addend)
        k >>= 1
    return result


def public_x_bytes(private_key: bytes) -> bytes:
    """28-byte big-endian X coordinate of privateKey * G."""
    d = int.from_bytes(private_key, "big")
    if not 1 <= d < N:
        raise ValueError("private key scalar out of range [1, n-1]")
    x, y, z = _scalar_mult(d, (GX, GY))
    if z == 0:
        raise ValueError("point at infinity")
    x_affine = (x * _inv(z) * _inv(z)) % P
    return x_affine.to_bytes(28, "big")


# ---------------------------------------------------------------------------
# Random / deterministic key material
# ---------------------------------------------------------------------------


class HmacDrbg:
    """Minimal HMAC-SHA256 DRBG so --seed reproduces the same key set."""

    def __init__(self, seed: bytes):
        self._k = b"\x00" * 32
        self._v = b"\x01" * 32
        self._update(seed)

    def _update(self, data: bytes):
        self._k = hmac.new(self._k, self._v + b"\x00" + data, hashlib.sha256).digest()
        self._v = hmac.new(self._k, self._v, hashlib.sha256).digest()
        if data:
            self._k = hmac.new(self._k, self._v + b"\x01" + data, hashlib.sha256).digest()
            self._v = hmac.new(self._k, self._v, hashlib.sha256).digest()

    def randbelow(self, limit: int) -> int:
        """Uniform value in [0, limit) via rejection sampling."""
        nbytes = (limit.bit_length() + 7) // 8
        while True:
            byte = b""
            while len(byte) < nbytes:
                self._v = hmac.new(self._k, self._v, hashlib.sha256).digest()
                byte += self._v
            value = int.from_bytes(byte[:nbytes], "big")
            if value < limit:
                return value


def new_private_key(rng) -> bytes:
    """Random (or DRBG-derived) scalar in [1, n-1] as 28 big-endian bytes."""
    return (1 + rng.randbelow(N - 1)).to_bytes(28, "big")


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

# Seconds since the Cocoa reference date 2001-01-01T00:00:00Z, the encoding
# PropertyListEncoder produces for Date with the default .deferredToDate.
_APPLE_REFERENCE_TIMESTAMP = time.mktime((2001, 1, 1, 0, 0, 0, 0, 0, 0))


def swift_int_id(public_key: bytes) -> int:
    """Stable 64-bit signed id from the public key (Swift's Int)."""
    digest = hashlib.sha256(public_key).digest()
    value = int.from_bytes(digest[:8], "big")
    return value - (1 << 64) if value >= (1 << 63) else value


def build_accessory(index: int, private_key: bytes, public_key: bytes) -> dict:
    """Accessory dict matching OpenHaystack's Accessory CodingKeys."""
    symmetric_key = hashlib.sha256(b"findyou-symmetric" + public_key).digest()
    return {
        # Fields Accessory.init(from:) decodes as required
        "name": f"FindYou {index:04d}",
        "id": swift_int_id(public_key),
        "privateKey": private_key,
        # Optional fields with decode fallbacks; provided for a clean import
        "symmetricKey": symmetric_key,
        "usesDerivation": False,
        "oldestRelevantSymmetricKey": symmetric_key,
        "lastDerivationTimestamp": time.time() - _APPLE_REFERENCE_TIMESTAMP,
        "updateInterval": 60.0 * 60 * 24,
        "colorComponents": [((index * 37) % 256) / 255.0, 0.75, 1.0, 1.0],
        "colorSpaceName": "kCGColorSpaceSRGB",
        "icon": "mappin",
        "isDeployed": True,
        "isActive": False,
    }


def write_c_array(public_keys, path):
    with open(path, "w") as f:
        f.write("static uint8_t public_keys[][28] = {\n")
        for key in public_keys:
            f.write("    {" + ", ".join(f"0x{b:02x}" for b in key) + "},\n")
        f.write("};\n")


def main():
    parser = argparse.ArgumentParser(
        description="Generate EC key pairs for the Find You firmware and retrieval app."
    )
    parser.add_argument("count", nargs="?", type=int, default=2000,
                        help="number of key pairs (default: 2000)")
    parser.add_argument("--seed", metavar="HEX",
                        help="hex seed for deterministic key generation")
    args = parser.parse_args()

    if args.count < 1:
        sys.exit("count must be >= 1")

    rng = HmacDrbg(bytes.fromhex(args.seed)) if args.seed else secrets

    private_keys, public_keys = [], []
    print(f"Generating {args.count} key pairs on secp224r1...")
    for _ in range(args.count):
        private_key = new_private_key(rng)
        private_keys.append(private_key)
        public_keys.append(public_x_bytes(private_key))

    ids = [swift_int_id(pub) for pub in public_keys]
    if len(set(ids)) != len(ids):
        sys.exit("collision in accessory ids; rerun with a different seed")

    write_c_array(public_keys, "pub_keys_c.txt")
    print(f"Wrote pub_keys_c.txt ({args.count} public keys)")

    accessories = [build_accessory(i, priv, pub)
                   for i, (priv, pub) in enumerate(zip(private_keys, public_keys), 1)]
    with open("accessory_list.plist", "wb") as f:
        plistlib.dump(accessories, f)
    print(f"Wrote accessory_list.plist ({args.count} accessories)")


if __name__ == "__main__":
    main()
