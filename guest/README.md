# pveosi-guest-setup (standalone guest bundle)

Install **inside the guest** (no Proxmox host paths required).

## Version-pinned install from GitHub

Pick a reviewed commit or tag, download the source archive, verify its checksum, then run the guest installer:

```bash
REF=580e43a8202a85186584fa281276a1fcbb15e76e
URL="https://github.com/faridze/proxmox-os-installer/archive/${REF}.tar.gz"
curl -fsSLo /tmp/pveosi-guest.tgz "$URL"
echo '<paste-sha256-from-release-notes>  /tmp/pveosi-guest.tgz' | sha256sum -c -
tar -xzf /tmp/pveosi-guest.tgz -C /tmp
cd "/tmp/proxmox-os-installer-${REF}"
sudo bash guest/install.sh
```

`guest/install.sh` installs the wrapper to `/usr/local/sbin/pveosi-guest-setup` and Python modules under `/usr/local/lib/pveosi-guest/`. The wrapper resolves `guest_setup_engine.py` beside itself or under that library path (no manual `PYTHONPATH` editing required).

## Flat copy (same layout as the installer)

```bash
sudo install -d -m 0755 /usr/local/lib/pveosi-guest
sudo install -m 0755 bin/pveosi-guest-setup /usr/local/sbin/pveosi-guest-setup
sudo install -m 0644 lib/guest_setup_engine.py lib/guest_netplan.py lib/guest_network.py \
  /usr/local/lib/pveosi-guest/
```

## Workflow (netplan guests)

```bash
sudo pveosi-guest-setup --preflight
sudo pveosi-guest-setup prepare          # netplan independence + cloud-init.disabled
sudo reboot
sudo pveosi-guest-setup verify           # interactive; machine-verified readiness
sudo pveosi-guest-setup --status
```

`prepare --dry-run` / top-level `--dry-run` performs no mutations.

**ifupdown / systemd-networkd:** read-only diagnostics; `cloud-init.disabled` is not created automatically. Use `verify --manual-only` only after manual network checks — this does **not** authorize cloud-init package removal.

Supported: Debian 12/13, Ubuntu 22.04/24.04 with netplan (safe copy to `90-pveosi-static.yaml`).
