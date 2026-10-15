# Proxmox OS Installer

Fast interactive cloud-image VM provisioning for **Proxmox VE 8+ (x86_64)**. Build Debian, Ubuntu, AlmaLinux, or Rocky Linux VMs without running the ISO installer. Uses official upstream images, cloud-init, and Proxmox `qm` with any active image-capable storage (including `local-lvm`).

> **Status: v0.2.3 (self-update + experimental first-boot SSH password support).** Offline mocked tests cover provisioning safety paths; **no supported OS has been fully boot-tested in CI** (CI never creates a real VM). Validate on a lab node using [docs/MANUAL_VALIDATION.md](docs/MANUAL_VALIDATION.md) before production use.

## Quick install from GitHub (on a Proxmox node, as root)

```bash
curl -fsSLo /root/proxmox-os-installer-bootstrap.sh \
  https://raw.githubusercontent.com/faridze/proxmox-os-installer/main/bootstrap.sh
bash /root/proxmox-os-installer-bootstrap.sh
```

The bootstrap invokes the downloaded install script through Bash to avoid archive file-mode and noexec temporary-directory issues. Review the bootstrap script before running it as root. It downloads the repository archive over HTTPS and installs local CLI files only; it does not install host packages or change cluster networking.

**Pinned / production installs:** set `PROXMOX_OS_INSTALLER_REF` to a reviewed tag or commit:

```bash
PROXMOX_OS_INSTALLER_REF=<tag-or-commit> bash /root/proxmox-os-installer-bootstrap.sh
```

For a fully pinned rollout, fetch the bootstrap script from the same ref (not only `main`).

Bootstrap and install record provenance in `install-metadata.json` under the install root (version, commit SHA when known, repository, install ref/channel, timestamp). This metadata drives safe update checks.

**Existing v0.2.2 (or older) installs** without metadata must run bootstrap or `install.sh` from a reviewed tree **once** before `pveosi update` is available.

## Update the installed tool

From **v0.2.3+**, use the built-in updater (HTTPS download from the official GitHub repository only; no `curl | bash` during update):

```bash
pveosi update --check          # read-only: show installed vs available target
pveosi update                  # interactive confirmation before install
pveosi update --yes            # skip confirmation only
pveosi update --ref <tag-or-commit>   # install a specific reviewed ref
```

**Update channels**

- Installs that track `main` check `main` for updates.
- Installs pinned to a **commit** or **non-main branch** stay on that channel; they are **not** silently switched to `main`. Use `--ref` to move to another ref (including `main`) explicitly.
- If provenance is unknown (no metadata), specify `--ref` instead of guessing.

**Rollback:** each update stages a full tree, validates it, then swaps the install directory. The previous install is kept as `proxmox-os-installer.previous` until the next successful update; if activation fails, the prior tree is restored automatically.

**Security:** updates trust GitHub over TLS; there are no detached signatures yet. Pin refs for production. Updates touch only this application's files under `/usr/local` and metadata; they do **not** modify VMs, image cache, snippets, or Proxmox storage/network settings.

Alternative: re-run bootstrap after review, or from a git checkout:

```bash
cd proxmox-os-installer && sudo ./install.sh
```

## Create a VM

After installation, prefer the short command **`pveosi`** (or **`pve-osi`** if `pveosi` is already taken on your node). The original **`proxmox-os-installer`** name remains available.

```bash
pveosi
```

The wizard uses a **two-level OS menu** (distribution, then version), then prompts for VM ID/name, CPU, memory, disk, storage, bridge, optional VLAN, DHCP or static IPv4, DNS, cloud-init user, and SSH public key (preferred) or optional login password. Defaults: **8 vCPU, 24 GiB RAM, 150 GiB disk**, **`local-lvm`** when available, **`vmbr0`** when present. Active storages and bridges are discovered from the node where possible.

An explicit confirmation is required before any download or VM creation.

### Commands

`pveosi` is installed when the name is free on your system. If another program already provides `pveosi`, the installer uses `pve-osi` instead. If both short names are taken, only `proxmox-os-installer` is linked.

```bash
pveosi                              # interactive provisioning (preferred)
pveosi --dry-run                    # validate inputs; no download/VM changes
pveosi --list-os                    # flat OS list
pveosi --check-catalog              # validate local catalog file
pveosi --check-upstream             # probe official manifest/image URLs (no full image download)
pveosi --cache-list                 # list checksum-verified cache files and usage
pveosi --cache-prune                # preview obsolete cache entries; confirm before delete
pveosi --version
pveosi update [--check|--ref REF|--yes]
# Equivalent: pve-osi … or proxmox-os-installer …
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

## Password SSH / optional root login (experimental v0.2.2)

For **new VMs before their first boot**, the wizard can enable SSH password login if a password was provided. It prompts explicitly before allowing **direct root SSH password login**, which switches the Cloud-Init login account to `root` and uses the selected login password. Use this only on restricted/trusted management networks. SSH keys remain the preferred approach.

This opt-in feature requires an active Proxmox storage supporting **snippets** (e.g., enable the `Snippets` content type on the `local` directory storage under Datacenter > Storage). It is **separate** from the VM disk storage (`local-lvm` cannot store snippet files). The tool refuses to create the VM if the requested snippet storage cannot be resolved. It creates a private `pveosi-VMID-user.yaml` file, preserving Proxmox-generated hostname, user, password and SSH key settings, then attaches it via `qm set --cicustom user=...`. The snippet must remain available on the VM's node, including after migration.

Cloud-Init normally applies first-boot user-data **once per instance**. This feature does not change SSH settings of an already booted VM; use its guest console/SSH access to adjust existing installations. Successful configuration does not guarantee guest-side success until verified after boot. `qm cloudinit dump VMID user` may show the Proxmox-generated config, not the attached custom snippet; inspect the snippet if necessary (it is root-only and may contain password hashes).

Proxmox `qm set --cipassword` may transiently expose the password in host process arguments. Restrict access to the node and prefer SSH keys. Full root-password SSH is not enabled by default.
