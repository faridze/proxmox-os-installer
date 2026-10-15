# Manual Proxmox validation matrix

Run on a **non-production** Proxmox VE 8+ node after installing from your feature branch. Offline CI does not create real VMs.

| OS ID | Download + checksum | VM creates | Cloud-init IP | SSH key login | Notes |
|-------|--------------------|------------|---------------|---------------|-------|
| debian-12 | | | | | |
| debian-13 | | | | | |
| ubuntu-22.04 | | | | | |
| ubuntu-24.04 | | | | | |
| almalinux-9 | | | | | |
| almalinux-10 | | | | | |
| rocky-9 | | | | | |
| rocky-10 | | | | | |

## Checklist per OS

1. `proxmox-os-installer --check-upstream` shows `OK` for the row (or diagnose `NETWORK` vs `FAIL`).
2. Dry-run: `proxmox-os-installer --dry-run` completes with expected summary.
3. Create VM with SSH public key, DHCP, default resources.
4. `qm config <vmid>` shows `scsi0`, `ide2`, `ipconfig0`, `sshkeys`.
5. Start VM; confirm serial (`qm terminal`) or console; verify `ip a` / DHCP.
6. Optional: static IP + custom DNS row.
7. Confirm disk size (`qm config`, guest `lsblk`).
8. Destroy test VM manually when done: `qm destroy <vmid> --purge` (only after explicit confirmation).

## Safety checks

- Attempt duplicate VM ID → must abort before mutation.
- Use intentionally small storage (test pool) → preflight error or thin-provision warning.
- Interrupt download (Ctrl+C) → no automatic VM deletion; cache lock released.

Record Proxmox version, node name, storage type, and date in the table before marking a row complete.
