#!/usr/bin/env python3
"""Parse an inert OS catalog and strict upstream SHA256/SHA512 checksum manifests.

No code is executed from the catalog, and URLs/filenames must be safe to interpolate
as fixed argument-vector elements (never shell-evaluated).
"""
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ID = re.compile(r"[a-z][a-z0-9.-]{0,31}\Z")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,30}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
HEX = re.compile(r"[0-9a-fA-F]+\Z")


def load_catalog(path):
    entries = []
    seen = set()
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        fields = raw.split("|")
        if len(fields) != 7:
            raise ValueError(f"catalog line {number}: expected 7 columns")
        os_id, label, user, base, image, manifest, algo = fields
        url = urlsplit(base)
        if not ID.fullmatch(os_id) or os_id in seen:
            raise ValueError(f"catalog line {number}: invalid or duplicate OS ID")
        if not 2 <= len(label) <= 70 or any(ord(c) < 32 for c in label):
            raise ValueError(f"catalog line {number}: invalid label")
        if not USER.fullmatch(user):
            raise ValueError(f"catalog line {number}: invalid default user")
        if not (url.scheme == "https" and url.hostname and not url.username
                and not url.password and not url.port and not url.query
                and not url.fragment and base.rstrip("/") == base and ".." not in url.path):
            raise ValueError(f"catalog line {number}: use a plain HTTPS base URL")
        if not NAME.fullmatch(image) or not NAME.fullmatch(manifest):
            raise ValueError(f"catalog line {number}: invalid filenames")
        if algo not in ("sha256", "sha512"):
            raise ValueError(f"catalog line {number}: unsupported checksum algorithm")
        entries.append(fields)
        seen.add(os_id)
    if not entries:
        raise ValueError("OS catalog is empty")
    return entries


def find_digest(manifest_file, image_name, algo):
    """Support GNU sha*sum lists and BSD `SHA256 (name) = hash` lists.

    A conflicting or missing digest is a hard error; all other filenames ignored.
    """
    if not NAME.fullmatch(image_name) or algo not in ("sha256", "sha512"):
        raise ValueError("invalid checksum lookup arguments")
    size = 64 if algo == "sha256" else 128
    matches = set()
    for line in Path(manifest_file).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        # GNU coreutils format: <digest><space><space-or-asterisk><name>
        m = re.fullmatch(r"([0-9a-fA-F]{%d})\s+\*?(\S+)" % size, line)
        if m:
            digest, filename = m.groups()
            if filename.removeprefix("./") == image_name:
                matches.add(digest.lower())
            continue
        # BSD format: SHA256 (filename) = <digest>
        m = re.fullmatch(r"(SHA256|SHA512) \(([^)]+)\)\s*=\s*([0-9a-fA-F]+)", line, re.I)
        if m:
            kind, filename, digest = m.groups()
            if kind.lower() == algo and filename.removeprefix("./") == image_name:
                if len(digest) != size:
                    raise ValueError("digest length mismatch")
                matches.add(digest.lower())
    if len(matches) != 1:
        raise ValueError(f"expected one unambiguous {algo} digest for {image_name}, found {len(matches)}")
    return matches.pop()


def main():
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "catalog-check":
            print(f"OK: {len(load_catalog(sys.argv[2]))} supported OS versions")
        elif len(sys.argv) == 5 and sys.argv[1] == "digest":
            print(find_digest(sys.argv[2], sys.argv[3], sys.argv[4]))
        else:
            print("Usage: image_catalog.py catalog-check CATALOG | digest MANIFEST IMAGE ALGO", file=sys.stderr)
            return 2
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
