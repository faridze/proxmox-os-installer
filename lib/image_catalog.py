#!/usr/bin/env python3
"""Parse an inert OS catalog and strict upstream SHA256/SHA512 checksum manifests.

No code is executed from the catalog, and URLs/filenames must be safe to interpolate
as fixed argument-vector elements (never shell-evaluated).
"""
import re
import sys
import urllib.error
import urllib.request
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


class UpstreamNetworkError(RuntimeError):
    """Transport failure reaching upstream (not an unsupported image)."""


MAX_MANIFEST_BYTES = 2 * 1024 * 1024
USER_AGENT = "proxmox-os-installer/0.2.1"


def _head_url(url, timeout=20):
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, method="HEAD", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        if exc.code in (405, 501):
            return exc.code
        return exc.code
    except urllib.error.URLError as exc:
        raise UpstreamNetworkError(str(exc.reason)) from exc


def _download_manifest(url, timeout=20):
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                return resp.status, None
            data = resp.read(MAX_MANIFEST_BYTES + 1)
            if len(data) > MAX_MANIFEST_BYTES:
                raise ValueError("manifest exceeds size limit")
            return resp.status, data
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except urllib.error.URLError as exc:
        raise UpstreamNetworkError(str(exc.reason)) from exc


def check_upstream(catalog_path):
    import tempfile

    entries = load_catalog(catalog_path)
    failures = 0
    for os_id, _label, _user, base, image, manifest, algo in entries:
        manifest_url = f"{base}/{manifest}"
        image_url = f"{base}/{image}"
        try:
            status, body = _download_manifest(manifest_url)
        except UpstreamNetworkError as exc:
            print(f"NETWORK {os_id}: {exc}")
            failures += 1
            continue
        except ValueError as exc:
            print(f"FAIL {os_id}: malformed manifest ({manifest_url}): {exc}")
            failures += 1
            continue
        if status >= 400 or body is None:
            print(f"FAIL {os_id}: checksum manifest HTTP {status} ({manifest_url})")
            failures += 1
            continue
        digest_ok = False
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(body)
                tmp_path = tmp.name
            find_digest(tmp_path, image, algo)
            digest_ok = True
        except (ValueError, OSError) as exc:
            print(f"FAIL {os_id}: manifest digest for {image}: {exc}")
            failures += 1
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
        if not digest_ok:
            continue
        try:
            i_status = _head_url(image_url)
        except UpstreamNetworkError as exc:
            print(f"NETWORK {os_id}: image probe: {exc}")
            failures += 1
            continue
        if i_status >= 400:
            print(f"FAIL {os_id}: image HEAD HTTP {i_status} ({image_url})")
            failures += 1
            continue
        print(f"OK   {os_id}: manifest digest verified; image URL reachable ({algo})")
    if failures:
        raise ValueError(f"{failures} upstream check(s) failed")
    return len(entries)


def distro_groups(catalog_path):
    order = ("debian", "ubuntu", "almalinux", "rocky")
    labels = {
        "debian": "Debian",
        "ubuntu": "Ubuntu",
        "almalinux": "AlmaLinux",
        "rocky": "Rocky Linux",
    }
    entries = load_catalog(catalog_path)
    groups = {k: [] for k in order}
    for row in entries:
        os_id = row[0]
        family = os_id.split("-", 1)[0]
        if family in groups:
            groups[family].append(row)
    for family in order:
        if groups[family]:
            print(f"{family}|{labels[family]}|{len(groups[family])}")


def main():
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "catalog-check":
            print(f"OK: {len(load_catalog(sys.argv[2]))} supported OS versions")
        elif len(sys.argv) == 3 and sys.argv[1] == "check-upstream":
            count = check_upstream(sys.argv[2])
            print(f"OK: {count} OS entries reachable")
        elif len(sys.argv) == 3 and sys.argv[1] == "distro-groups":
            distro_groups(sys.argv[2])
        elif len(sys.argv) == 5 and sys.argv[1] == "digest":
            print(find_digest(sys.argv[2], sys.argv[3], sys.argv[4]))
        else:
            print(
                "Usage: image_catalog.py catalog-check CATALOG | check-upstream CATALOG | "
                "distro-groups CATALOG | digest MANIFEST IMAGE ALGO",
                file=sys.stderr,
            )
            return 2
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
