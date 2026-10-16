# shellcheck shell=bash
# User-facing guidance when Cloud-Init Snippets storage is missing (sourced by install.sh / installer).

pveosi_list_snippet_storages() {
  pvesm status --content snippets 2>/dev/null | awk 'NR>1 && $3=="active" {print $1}'
}

pveosi_local_storage_active() {
  pvesm status 2>/dev/null | awk 'NR>1 && $1=="local" && $3=="active" {found=1} END{exit !found}'
}

# Print actionable multiline error; exit 1. vm_created: 1 only if a VM was already created.
pveosi_snippets_missing_error() {
  local vm_created="${1:-0}"
  printf '%s\n' \
    'ERROR: SSH password login requires a storage' \
    '       with Cloud-Init Snippets support.' \
    '' >&2
  if pveosi_local_storage_active; then
    printf '%s\n' \
      'How to fix this in Proxmox:' \
      '' \
      '1. Open the Proxmox web interface.' \
      '2. Navigate to Datacenter > Storage.' \
      '3. Select the "local" storage.' \
      '4. Click Edit.' \
      '5. Under Content, enable "Snippets".' \
      '6. Keep all previously selected content types.' \
      '7. Save the changes.' \
      '' >&2
  else
    printf '%s\n' \
      'How to fix this in Proxmox:' \
      '' \
      '1. Open the Proxmox web interface.' \
      '2. Navigate to Datacenter > Storage.' \
      '3. Select an active directory or NFS storage that can host small files' \
      '   (not local-lvm; thin/block pools cannot store Snippets).' \
      '4. Click Edit.' \
      '5. Under Content, enable "Snippets".' \
      '6. Keep all previously selected content types.' \
      '7. Save the changes.' \
      '' \
      'If no suitable storage exists, add a directory or NFS storage first.' \
      '' >&2
  fi
  printf '%s\n' \
    'Verify on this node:' \
    '  pvesm status --content snippets' \
    '' \
    'Then run pveosi again.' \
    '' \
    'Note:' \
    '- VM disks may remain on local-lvm.' \
    '- Snippets are separate Cloud-Init configuration files.' \
    '- No storage configuration was changed automatically.' >&2
  if (( vm_created == 0 )); then
    printf '%s\n' '- No VM was created by this failed attempt.' >&2
  else
    printf '%s\n' '- A VM may already exist from this attempt; inspect with qm config before retrying.' >&2
  fi
  exit 1
}

pveosi_require_snippet_storage() {
  local vm_created="${1:-0}"
  local -a candidates=()
  mapfile -t candidates < <(pveosi_list_snippet_storages)
  if (( ${#candidates[@]} == 0 )); then
    pveosi_snippets_missing_error "$vm_created"
  fi
}
