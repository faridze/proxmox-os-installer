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

## New in v0.2.2: password SSH / optional root SSH first boot

Only on a **fresh disposable VM**: skip SSH public key, set a password, accept password SSH, explicitly opt into root SSH if desired, and choose an active snippets storage (e.g., local with snippets enabled). Inspect `qm config VMID` for `cicustom: user=STORAGE:snippets/pveosi-VMID-user.yaml`. Check that the snippet is `0600` and the `user` field matches the selected login. Before marking PASS, wait for `cloud-init status --wait`, then confirm `sshd -T | grep -Ei 'passwordauthentication|permitrootlogin'` inside the guest and test a real password login from an allowed management IP.

If testing root SSH, confirm that the **root account has a usable password** and login works after boot; do not infer success solely from `qm config`. Ensure SSH port 22 is firewalled from the public Internet. Verify guest log shows no cloud-init failures. For existing booted VMs, configure via guest console/SSH; Cloud-Init settings are not generally replayed on reboot.

Do not delete the attached `snippets` file: the VM needs it for subsequent starts/migration, and VM deletion does not automatically clean it up.

## v0.2.4: Debian 13 root password SSH + finalize (lab plan)

### Phase A — first boot (fresh VM)

1. Install/update to the branch commit on the lab node (`pveosi update --ref <commit>` or `install.sh` from checkout).
2. Create a **Debian 13** VM **without** an SSH public key; explicitly enable **root SSH password** login; select snippets storage with Snippets enabled.
3. Complete first boot; run `cloud-init status --wait` in the guest.
4. Verify PuTTY/SSH root login with the initial password.
5. Change the root password inside the guest; confirm the old password fails and the new password works.

### Phase B — optional Cloud-Init finalization

1. Inside the guest: create `/etc/cloud/cloud-init.disabled`, reboot, verify network + SSH, shut down cleanly.
2. On the host (VM stopped): `pveosi finalize <VMID> --dry-run` and review the preview.
3. Run `pveosi finalize <VMID>`; attest guest preparation; approve host-side changes and optional snippet removal.
4. Start the VM; verify networking, SSH, current password, and filesystems.

### Inspecting failed VM 121 (do not boot or destroy)

Read-only inspection on the Proxmox node:

```bash
qm config 121
qm status 121
ls -la "$(pvesm path local:snippets/pveosi-121-user.yaml 2>/dev/null)" 2>/dev/null || true
```

Do **not** boot, destroy, or modify VM 121 unless you explicitly decide to recover or remove it outside this checklist.
