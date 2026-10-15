# Proxmox OS Installer

Fast interactive cloud-image VM provisioning for **Proxmox VE 8+ (x86_64)**. Build Debian, Ubuntu, AlmaLinux, or Rocky Linux VMs without running the ISO installer. Uses official upstream images, cloud-init, and Proxmox `qm` with any active image-capable storage (including `local-lvm`).

> **Status: v0.2.0.** Offline mocked tests cover provisioning safety paths; **no supported OS has been fully boot-tested in CI** (CI never creates a real VM). Validate on a lab node using [docs/MANUAL_VALIDATION.md](docs/MANUAL_VALIDATION.md) before production use.

## Quick install from GitHub (on a Proxmox node, as root)

```bash
curl -fsSLo /root/proxmox-os-installer-bootstrap.sh \
  https://raw.githubusercontent.com/faridze/proxmox-os-installer/main/bootstrap.sh
bash /root/proxmox-os-installer-bootstrap.sh
```

Review the bootstrap script before running it as root. It downloads the repository archive over HTTPS and installs local CLI files only; it does not install host packages or change cluster networking.

**Pinned / production installs:** set `PROXMOX_OS_INSTALLER_REF` to a reviewed tag or commit:

```bash
PROXMOX_OS_INSTALLER_REF=<tag-or-commit> bash /root/proxmox-os-installer-bootstrap.sh
```

For a fully pinned rollout, fetch the bootstrap script from the same ref (not only `main`).

## Update the installed tool

Re-run the bootstrap (after reviewing upstream changes), or from a git checkout:

```bash
cd proxmox-os-installer && sudo ./install.sh
```

## Create a VM

```bash
proxmox-os-installer
```

The wizard uses a **two-level OS menu** (distribution, then version), then prompts for VM ID/name, CPU, memory, disk, storage, bridge, optional VLAN, DHCP or static IPv4, DNS, cloud-init user, and SSH public key (preferred) or optional login password. Defaults: **8 vCPU, 24 GiB RAM, 150 GiB disk**, **`local-lvm`** when available, **`vmbr0`** when present. Active storages and bridges are discovered from the node where possible.

An explicit confirmation is required before any download or VM creation.

### Commands

```bash
proxmox-os-installer                 # interactive provisioning
proxmox-os-installer --dry-run       # validate inputs; no download/VM changes
proxmox-os-installer --list-os       # flat OS list
proxmox-os-installer --check-catalog # validate local catalog file
proxmox-os-installer --check-upstream # probe official manifest/image URLs (no full image download)
proxmox-os-installer --cache-list    # list checksum-verified cache files and usage
proxmox-os-installer --cache-prune     # preview obsolete cache entries; confirm before delete
proxmox-os-installer --version
```

## Supported OS versions

| OS | Versions | Default user |
|---|---|---|
| Debian | 12, 13 | `debian` |
| Ubuntu Server | 22.04 LTS, 24.04 LTS | `ubuntu` |
| AlmaLinux | 9, 10 | `almalinux` |
| Rocky Linux | 9, 10 | `rocky` |

Catalog: `catalog/os-images.tsv`. Run `proxmox-os-installer --check-upstream` to verify mirrors; `NETWORK` lines indicate connectivity problems, `FAIL` indicates missing or unsupported upstream paths.

## Image cache management

Cache directory (default): `/var/cache/proxmox-os-installer/images`

- Images are reused only after verification against the **current** upstream checksum manifest.
- Downloads use file locks (`$OS_ID.lock`) to avoid concurrent corruption.
- `--cache-prune` never deletes files covered by an active lock or in-progress temp names (`.image.*`, `.checksums.*`).

## Provisioning safety (v0.2.0)

- VM ID checked before prompts and again immediately before `qm create`.
- Proxmox command failures abort with the failed operation, VM ID, and **manual** recovery hints (`qm config`, optional `qm destroy --purge` only if you explicitly intend to).
- Storage must be **active** and support VM **images** (including cloud-init drives).
- Non-thin storage: errors when reported free space is below requested disk size.
- Thin / shared pools: **warnings** that reported free space is not a capacity guarantee.
- Image virtual size must not exceed requested disk size; checksum and format validated before import.
- Post-create verification ensures `scsi0` and cloud-init `ide2` appear in `qm config`.
- **Never** overwrites an existing VM ID; **never** auto-deletes a partial VM.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `VM ID already in use` | `qm list` / `qm config <id>`; pick another ID |
| Storage preflight failed | `pvesm status`, ensure `images` content is active |
| Thin pool warning | Ensure sufficient real capacity; thin oversubscription can still fail at runtime |
| Checksum mismatch | Re-run; delete bad cache file only via `--cache-prune` after review |
| Partial VM after error | `qm config <id>`; destroy manually only if intended |
| `--check-upstream` NETWORK | DNS/firewall; distinguish from unsupported catalog URLs |
| SSH login fails | Use SSH keys; many images disable password SSH |
| Guest IP missing | Cloud-init network, bridge/VLAN, DHCP server |

## Security limitations

- HTTPS + manifest checksums; **no detached GPG manifest verification** yet.
- Bootstrap trusts GitHub TLS; pin refs for controlled rollouts.
- `qm set --cipassword` may expose passwords briefly in process listings; **SSH keys are strongly preferred**.
- QEMU Guest Agent is enabled in VM config only; install/start the agent in the guest separately.
- Password-based SSH may be disabled in upstream cloud images even when a password is set.

## Development / CI

```bash
git clone https://github.com/faridze/proxmox-os-installer.git
cd proxmox-os-installer
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n bootstrap.sh install.sh bin/proxmox-os-installer
sudo bash tests/test_dry_run.sh
sudo bash tests/test_provision.sh
sudo bash tests/test_provision_flow.sh
sudo bash tests/test_provision_failures.sh
```

CI uses mocked `qm`/`pvesm` and **does not create real VMs**.

## Manual Proxmox validation

See [docs/MANUAL_VALIDATION.md](docs/MANUAL_VALIDATION.md) for the per-OS matrix and checklist.

License: MIT.
