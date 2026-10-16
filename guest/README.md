# pveosi-guest-setup (standalone guest bundle)

Copy this directory's sibling files into a VM **without** installing the full Proxmox host tool:

```bash
# On your workstation (from a clone of this repository):
scp bin/pveosi-guest-setup lib/guest_setup_engine.py user@VM:/tmp/pveosi-guest/
ssh user@VM 'sudo install -m 0755 /tmp/pveosi-guest/pveosi-guest-setup /usr/local/sbin/pveosi-guest-setup'
ssh user@VM 'sudo install -m 0644 /tmp/pveosi-guest/guest_setup_engine.py /usr/local/lib/pveosi-guest-setup.py'
```

Edit `/usr/local/sbin/pveosi-guest-setup` to point `ENGINE` at `/usr/local/lib/pveosi-guest-setup.py`, or keep the two-file layout from `bin/` + `lib/`.

Run inside the guest as root:

```bash
sudo pveosi-guest-setup --preflight
sudo pveosi-guest-setup --guided
```

Supported: Debian 12/13, Ubuntu Server 22.04/24.04 LTS (systemd, netplan/ifupdown/systemd-networkd). NetworkManager-active guests are refused for automated network changes.

This utility does **not** SSH from the Proxmox host; operators copy and run it manually after first boot.
