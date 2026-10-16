#!/usr/bin/env python3
"""Structured guest network facts (ip -j) for reboot verification."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


class NetworkCompareError(Exception):
    """Network verification failure."""


@dataclass
class IfaceFacts:
    name: str
    addresses: list[str]  # CIDR strings
    addressing: str  # dhcp | static | unknown


@dataclass
class NetworkFacts:
    interfaces: list[IfaceFacts]
    default_gw: Optional[str]
    default_dev: Optional[str]
    default_metric: Optional[int]


@dataclass
class NetworkCompareResult:
    ok: bool
    needs_operator_confirm: bool
    messages: list[str] = field(default_factory=list)


def _root() -> Path:
    import os

    return Path(os.environ.get("PVEOSI_GUEST_ROOT", "/"))


def _run_ip_json(args: list[str]) -> Any:
    import os

    if os.environ.get("PVEOSI_GUEST_MOCK_CMD") == "1":
        key = " ".join(args)
        raw = os.environ.get(f"PVEOSI_GUEST_MOCK_IPJ_{key.replace(' ', '_')}", "")
        if not raw and args[:1] == ["route"]:
            raw = os.environ.get("PVEOSI_GUEST_MOCK_IPJ_ROUTE", "[]")
        if not raw and args[:1] == ["addr"]:
            raw = os.environ.get("PVEOSI_GUEST_MOCK_IPJ_ADDR", "[]")
        if not raw:
            raw = "[]"
        return json.loads(raw)
    cmd = ["ip", "-j"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise NetworkCompareError("ip -j command failed.")
    return json.loads(r.stdout or "null")


def _netplan_addressing_hints() -> dict[str, str]:
    """Map interface name -> dhcp | static | unknown from active netplan YAML."""
    hints: dict[str, str] = {}
    np_dir = _root() / "etc/netplan"
    if not np_dir.is_dir() or yaml is None:
        return hints
    files = sorted(np_dir.glob("*.yaml"))
    for path in files:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        net = doc.get("network") if isinstance(doc, dict) else None
        if not isinstance(net, dict):
            continue
        eth = net.get("ethernets")
        if not isinstance(eth, dict):
            continue
        for name, cfg in eth.items():
            if not isinstance(cfg, dict):
                hints[str(name)] = "unknown"
                continue
            dhcp4 = cfg.get("dhcp4")
            dhcp6 = cfg.get("dhcp6")
            if dhcp4 is True or dhcp6 is True:
                hints[str(name)] = "dhcp"
            elif cfg.get("addresses"):
                hints[str(name)] = "static"
            else:
                hints[str(name)] = "unknown"
    return hints


def _resolve_addressing(
    name: str, hints: dict[str, str], runtime_dynamic: bool
) -> str:
    hinted = hints.get(name, "unknown")
    if hinted == "dhcp":
        return "dhcp"
    if hinted == "static":
        return "static"
    if runtime_dynamic:
        return "dhcp"
    return "unknown"


def capture_network_facts() -> NetworkFacts:
    addr_data = _run_ip_json(["addr", "show"])
    route_data = _run_ip_json(["route", "show", "default"])
    hints = _netplan_addressing_hints()
    ifaces: list[IfaceFacts] = []
    if isinstance(addr_data, list):
        for item in addr_data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("ifname", ""))
            if not name or name == "lo":
                continue
            addrs: list[str] = []
            runtime_dynamic = False
            for ai in item.get("addr_info", []) or []:
                if not isinstance(ai, dict):
                    continue
                if ai.get("family") != "inet":
                    continue
                local = ai.get("local")
                prefix = ai.get("prefixlen")
                if local is not None and prefix is not None:
                    addrs.append(f"{local}/{prefix}")
                if ai.get("dynamic"):
                    runtime_dynamic = True
            addressing = _resolve_addressing(name, hints, runtime_dynamic)
            ifaces.append(
                IfaceFacts(name=name, addresses=sorted(addrs), addressing=addressing)
            )
    gw: Optional[str] = None
    dev: Optional[str] = None
    metric: Optional[int] = None
    if isinstance(route_data, list):
        for rt in route_data:
            if not isinstance(rt, dict):
                continue
            if rt.get("dst") == "default":
                gw = rt.get("gateway")
                dev = rt.get("dev")
                metric = rt.get("metric")
                break
    return NetworkFacts(
        interfaces=sorted(ifaces, key=lambda x: x.name),
        default_gw=str(gw) if gw else None,
        default_dev=str(dev) if dev else None,
        default_metric=int(metric) if metric is not None else None,
    )


def facts_to_checkpoint(facts: NetworkFacts) -> dict[str, Any]:
    return {
        "interfaces": [
            {"name": i.name, "addresses": i.addresses, "addressing": i.addressing}
            for i in facts.interfaces
        ],
        "default_gw": facts.default_gw,
        "default_dev": facts.default_dev,
        "default_metric": facts.default_metric,
    }


def facts_from_checkpoint(data: dict[str, Any]) -> NetworkFacts:
    ifaces = []
    for item in data.get("interfaces", []):
        addr_mode = item.get("addressing")
        if addr_mode is None and "dhcp" in item:
            addr_mode = "dhcp" if item.get("dhcp") else "static"
        if addr_mode not in ("dhcp", "static", "unknown"):
            addr_mode = "unknown"
        ifaces.append(
            IfaceFacts(
                name=str(item.get("name", "")),
                addresses=list(item.get("addresses", [])),
                addressing=str(addr_mode),
            )
        )
    return NetworkFacts(
        interfaces=ifaces,
        default_gw=data.get("default_gw"),
        default_dev=data.get("default_dev"),
        default_metric=data.get("default_metric"),
    )


def compare_network_facts(before: NetworkFacts, after: NetworkFacts) -> NetworkCompareResult:
    msgs: list[str] = []
    need_confirm = False
    before_map = {i.name: i for i in before.interfaces}
    after_map = {i.name: i for i in after.interfaces}
    if set(before_map) != set(after_map):
        msgs.append("Network interface set changed.")
        return NetworkCompareResult(False, False, msgs)
    for name, b in before_map.items():
        a = after_map[name]
        mode_b = b.addressing
        mode_a = a.addressing
        if mode_b == "static" and mode_a == "static":
            if b.addresses != a.addresses:
                msgs.append(f"Static IPv4 changed on {name}.")
                return NetworkCompareResult(False, False, msgs)
        elif mode_b == "dhcp" and mode_a == "dhcp":
            if b.addresses != a.addresses:
                msgs.append(f"DHCP address changed on {name}; operator confirmation required.")
                need_confirm = True
        else:
            if b.addresses != a.addresses:
                msgs.append(
                    f"Address changed on {name} (addressing mode {mode_b}->{mode_a}); "
                    "operator confirmation required."
                )
                need_confirm = True
    if before.default_gw != after.default_gw or before.default_dev != after.default_dev:
        if all(i.addressing == "dhcp" for i in before.interfaces) and all(
            i.addressing == "dhcp" for i in after.interfaces
        ):
            msgs.append("Default route changed on DHCP guest; operator confirmation required.")
            need_confirm = True
        else:
            msgs.append("Default route or gateway changed.")
            return NetworkCompareResult(False, False, msgs)
    if before.default_metric != after.default_metric:
        if all(i.addressing == "dhcp" for i in before.interfaces):
            need_confirm = True
        else:
            msgs.append("Default route metric changed.")
            return NetworkCompareResult(False, False, msgs)
    return NetworkCompareResult(True, need_confirm, msgs)


def check_dns_resolution() -> None:
    import os

    if os.environ.get("PVEOSI_GUEST_MOCK_CMD") == "1":
        if os.environ.get("PVEOSI_GUEST_MOCK_DNS_FAIL") == "1":
            raise NetworkCompareError("DNS resolution check failed (mock).")
        return
    r = subprocess.run(
        ["getent", "hosts", "example.com"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        raise NetworkCompareError("DNS resolution check failed (getent hosts example.com).")


def resolv_conf_usable() -> bool:
    rc = _root() / "etc/resolv.conf"
    if not rc.is_file():
        return False
    text = rc.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("nameserver") and not line.endswith("127.0.0.53"):
            return True
        if line.startswith("nameserver"):
            return True
    return bool(re.search(r"^nameserver\s+\S+", text, re.M))
