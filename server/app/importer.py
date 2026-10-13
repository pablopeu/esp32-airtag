"""Import tags from OpenHaystack-style plists or pasted key material.

Two sources are accepted:

- Upload of an ``accessory_list.plist`` as produced by ``generate_keypairs.py``
  (or exported by the OpenHaystack app): an array of Accessory dicts, each
  with a ``privateKey`` entry (28-byte secp224r1 scalar). When the plist
  contains many accessories that belong to one physical device (the usual
  case for our key-rotation firmware), they can be merged into a single tag.
- Raw private keys pasted as hex or base64, one per line.
"""

import base64
import binascii
import plistlib

from . import db


def _clean_b64(s: str) -> bytes:
    return base64.b64decode(s.strip(), validate=True)


def keys_from_plist(data: bytes) -> list[tuple[str, bytes]]:
    """Returns list of (accessory_name, private_key) from an uploaded plist."""
    plist = plistlib.loads(data)
    if isinstance(plist, dict):
        # Old-style export: {"accessories": [...]}
        plist = plist.get("accessories", [])
    out = []
    for acc in plist:
        if not isinstance(acc, dict) or "privateKey" not in acc:
            continue
        key = acc["privateKey"]
        if isinstance(key, str):
            key = _clean_b64(key)
        out.append((acc.get("name") or "accessory", bytes(key)))
    return out


def keys_from_text(text: str) -> list[bytes]:
    """Parse pasted private keys: hex or base64, one per line."""
    keys = []
    for line in text.splitlines():
        line = line.strip().replace(" ", "")
        if not line or line.startswith("#"):
            continue
        try:
            raw = bytes.fromhex(line)
        except ValueError:
            try:
                raw = base64.b64decode(line, validate=True)
            except (binascii.Error, ValueError):
                continue
        keys.append(raw)
    return keys


def import_plist(data: bytes, name: str | None, single_tag: bool) -> list[int]:
    """Import an uploaded plist. Returns the created tag ids."""
    entries = keys_from_plist(data)
    if not entries:
        raise ValueError("No accessories with a privateKey found in the plist")
    if single_tag or len(entries) == 1:
        tag_name = name or (entries[0][0] if len(entries) == 1 else f"{entries[0][0]} (+{len(entries) - 1})")
        return [db.create_tag(tag_name, [k for _, k in entries])]
    return [db.create_tag(n, [k]) for n, k in entries]


def import_text_keys(text: str, name: str) -> int:
    keys = keys_from_text(text)
    if not keys:
        raise ValueError("No valid private keys found (expected hex or base64, one per line)")
    return db.create_tag(name, keys)
