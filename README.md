# Proxmox OS Installer

Fast interactive cloud-image VM provisioning for **Proxmox VE 8+ (x86_64)**. Build Debian, Ubuntu, AlmaLinux or Rocky Linux VMs without running the ISO installer. Uses official upstream images, cloud-init, and Proxmox `qm` with any active image-capable storage (including `local-lvm`).

> **Status: v0.1.0 / initial release.** Offline tests pass; real VM deployment and all eight upstream downloads have not yet been end-to-end verified. First use should be on a test VM.

## Install in two commands (on a Proxmox node, as root)

```bash
curl -fsSLo /root/proxmox-os-installer-bootstrap.sh https://raw.githubusercontent.com/faridze/proxmox-os-installer/main/bootstrap.sh
bash /root/proxmox-os-installer-bootstrap.sh
```

Review the downloaded script before running it as root. The bootstrap downloads the GitHub repository archive over HTTPS and installs only the local CLI files; no packages/services are modified. To update in the future, run the same two commands again after reviewing upstream changes.

To launch the wizard:

```bash
proxmox-os-installer
```

A pinned reviewed Git tag or commit can be used to avoid floating `main` for installs: `PROXMOX_OS_INSTALLER_REF=v0.1.0 bash /root/proxmox-os-installer-bootstrap.sh` (only after that tag has actually been published). The bootstrap itself is retrieved from `main`, so for a fully pinned deployment also fetch the bootstrap from the pinned ref.

## Supported systems

| OS | Versions | Default user |
|---|---|---|
| Debian | 12, 13 | `debian` |
| Ubuntu Server | 22.04 LTS, 24.04 LTS | `ubuntu` |
| AlmaLinux | 9, 10 | `almalinux` |
| Rocky Linux | 9, 10 | `rocky` |

The OS choices live in `catalog/os-images.tsv`. Add new official cloud images + their checksum manifest there; the main VM workflow does not require edits for common `qcow2`/`raw` cloud images.

## Wizard

The wizard asks for the OS release, VM ID/name, vCPU count, memory, disk size, storage, bridge, optional VLAN, IPv4 DHCP or static IP/gateway/DNS, cloud-init username, and an SSH public key or password. Defaults are **8 vCPU, 24 GiB RAM, 150 GiB disk, `local-lvm`, `vmbr0`, DHCP**; everything is adjustable. An explicit confirmation is required before download and VM creation.

```bash
proxmox-os-installer              # interactive provisioning
proxmox-os-installer --dry-run    # validate inputs; do not download/create a VM
proxmox-os-installer --list-os    # list available distributions
proxmox-os-installer --check-catalog
proxmox-os-installer --version
```

The installer does not overwrite existing VM IDs, auto-delete incomplete VMs, change host networking, modify firewall rules or install host packages. Cache path: `/var/cache/proxmox-os-installer/images`. It reuses images only after validating them against the current upstream checksum manifest. Partially configured new VMs are left in place for inspection if a command fails.

**Console/login:** Cloud images may disable password-based SSH even when a console login password is configured; use SSH public keys for remote access. QEMU Guest Agent is enabled in the VM config but may need to be installed in the guest. Confirm guest partition growth with `lsblk`/`df -h` after boot.

## Manual install / development

```bash
git clone https://github.com/faridze/proxmox-os-installer.git
cd proxmox-os-installer
python3 -m unittest discover -s tests -v
bash -n bootstrap.sh install.sh bin/proxmox-os-installer
sudo ./install.sh
```

To make a new OS version available, edit `catalog/os-images.tsv` and run the tests. Catalog entry format:

```text
id|name|cloud_username|https_base_url|image_filename|checksum_manifest_filename|sha256_or_sha512
```

Check the filename and upstream checksum format before releasing a new entry. Entries with ISO-only installers are **not** supported by the generic cloud-image workflow.

## Security / current limitations

- Image fetches require HTTPS, follow HTTPS-only redirects, validate SHA256/SHA512 digests from the vendor manifest and recheck cached downloads. **Detached GPG verification of the vendor manifest is not yet implemented**; HTTPS + checksums alone is weaker than verifying an independently trusted signature.
- The bootstrap downloads GitHub source over HTTPS, but does not verify a project release signature. For controlled production rollouts, review and pin a commit or signed release first.
- Console passwords are passed to `qm set --cipassword` as supported by Proxmox; this can expose a password transiently in process arguments. Prefer an SSH key and skip the password where practical.
- Default CPU mode is `host` (best performance on a single node; consider migration compatibility on mixed-CPU clusters).
- No auto-template, unattended batch creation, or unattended updates yet.

License: MIT.
