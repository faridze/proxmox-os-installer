# pveosi-guest-setup (standalone guest bundle)

Version-pinned install **inside the guest** (no Proxmox host paths required):

```bash
# From a reviewed repository checkout (set REF to tag or commit):
REF=c042933c7bcd0a11206754cdffb452a70ea6f830
curl -fsSLo /tmp/pveosi-guest.tgz \
  "https://github.com/faridze/proxmox-os-installer/archive/${REF}.tar.gz"
tar -xzf /tmp/pveosi-guest.tgz -C /tmp
cd "/tmp/proxmox-os-installer-${REF}"
sudo bash guest/install.sh
```

Or copy two files into one directory on the guest (flat bundle):

```bash
install -d -m 0755 /opt/pveosi-guest
install -m 0755 bin/pveosi-guest-setup /opt/pveosi-guest/
install -m 0644 lib/guest_setup_engine.py lib/guest_netplan.py /opt/pveosi-guest/
ln -sf /opt/pveosi-guest/pveosi-guest-setup /usr/local/sbin/pveosi-guest-setup
export PYTHONPATH=/opt/pveosi-guest
```

The wrapper resolves `guest_setup_engine.py` next to itself or under `/usr/local/lib/pveosi-guest/`.

## Workflow

```bash
sudo pveosi-guest-setup --preflight
sudo pveosi-guest-setup prepare          # netplan independence + cloud-init.disabled
sudo reboot
sudo pveosi-guest-setup verify           # after reboot; attests external SSH
sudo pveosi-guest-setup --status
```

`prepare --dry-run` / top-level `--dry-run` performs no mutations.

Supported: Debian 12/13, Ubuntu 22.04/24.04 with netplan (safe copy to `90-pveosi-static.yaml`). ifupdown/systemd-networkd: read-only diagnostics only.
