#!/usr/bin/env python3
"""
Migration Eligibility Module for VMware MCP Server

Applies VMware->KVM/HVM migration rules on top of the raw signals already
exposed by vm_info.get_vm_details. Centralised so the rules don't drift
into LLM prompts.

Rules encoded here (hard blockers):
  - vSphere VM Encryption  -> KVM cannot decrypt the keys.
  - vTPM device attached    -> not portable across hypervisors.
  - PCI passthrough / vGPU  -> hardware doesn't exist on the target.
  - NVDIMM / persistent mem -> hardware-class device.
  - Fault Tolerance enabled -> FT pair is hypervisor-specific.
  - Physical-mode RDM disks -> bind to a specific LUN that won't be there.
  - Multi-writer shared disks -> shared semantics differ.
  - VMware Tools missing    -> Zerto needs Tools for quiesce + run_in_guest.

Soft warnings (don't block, but flag):
  - Snapshots present       -> consolidate before cutover.
  - Independent disks       -> break replication consistency.
  - Secure Boot enabled     -> KVM target must trust matching cert chain.
  - Firmware = bios on a host that requires UEFI (or vice versa)
  - Powered-off VM          -> initial sync may need it running.
  - Older hardware version  -> may need upgrade before move.
"""

from typing import Optional, List, Dict, Any
from pyVmomi import vim
import connection
import vm_info


# Default hardware-version floor below which we flag a soft warning. Adjust
# via the min_hw_version arg on the tool if you target a different KVM build.
DEFAULT_MIN_HW_VERSION = 13


def _hw_version_int(version_str: str) -> Optional[int]:
    """vmx-19 -> 19; vmx-13 -> 13; otherwise None."""
    if not version_str or not isinstance(version_str, str):
        return None
    if version_str.startswith('vmx-'):
        try:
            return int(version_str[4:])
        except ValueError:
            return None
    return None


def _gather_signals(vm) -> Dict[str, Any]:
    """Reuse the same extraction as vm_info to keep one source of truth."""
    signals: Dict[str, Any] = {
        'name': vm.name,
        'power_state': str(vm.runtime.powerState),
        'guest_id': vm.config.guestId if vm.config else 'unknown',
        'hardware_version': vm.config.version if vm.config else None,
        'tools_status': getattr(vm.guest, 'toolsRunningStatus', 'unknown') if vm.guest else 'unknown',
        'encrypted': bool(vm.config and getattr(vm.config, 'keyId', None) is not None),
        'firmware': getattr(vm.config, 'firmware', 'unknown') if vm.config else 'unknown',
        'secure_boot': False,
        'snapshot_count': vm_info._count_snapshots(
            vm.snapshot.rootSnapshotList if vm.snapshot else None
        ),
        'fault_tolerance_state': getattr(vm.runtime, 'faultToleranceState', 'unknown'),
    }

    boot_options = getattr(vm.config, 'bootOptions', None) if vm.config else None
    if boot_options is not None:
        signals['secure_boot'] = bool(getattr(boot_options, 'efiSecureBootEnabled', False))

    signals.update(vm_info._scan_devices_and_disks(vm))
    return signals


def _apply_rules(s: Dict[str, Any], min_hw_version: int) -> Dict[str, Any]:
    blockers: List[str] = []
    warnings: List[str] = []

    # ----- Hard blockers -----
    if s['encrypted']:
        blockers.append("vSphere VM Encryption is enabled (KVM cannot decrypt the keys)")
    if s.get('vtpm'):
        blockers.append("vTPM device is attached (not portable across hypervisors)")
    if s.get('pci_passthrough_count', 0) > 0:
        blockers.append(f"PCI passthrough / vGPU devices attached ({s['pci_passthrough_count']})")
    if s.get('nvdimm_count', 0) > 0:
        blockers.append(f"NVDIMM / persistent memory devices attached ({s['nvdimm_count']})")
    if s['fault_tolerance_state'] not in ('notConfigured', 'unknown'):
        blockers.append(f"Fault Tolerance is configured (state={s['fault_tolerance_state']})")
    if s.get('physical_rdm_count', 0) > 0:
        blockers.append(f"Physical-mode RDM disks present ({s['physical_rdm_count']})")
    if s.get('multi_writer_disk_count', 0) > 0:
        blockers.append(f"Multi-writer shared disks present ({s['multi_writer_disk_count']})")
    if s['tools_status'] == 'guestToolsNotRunning':
        blockers.append("VMware Tools is not running (Zerto needs Tools for quiesce + guest ops)")

    # ----- Soft warnings -----
    if s['snapshot_count'] > 0:
        warnings.append(f"Snapshots present ({s['snapshot_count']}); consolidate before cutover")
    if s.get('independent_disk_count', 0) > 0:
        warnings.append(f"Independent disks present ({s['independent_disk_count']}); replication consistency may break")
    if s['secure_boot']:
        warnings.append("Secure Boot is enabled; verify KVM target trusts the matching cert chain")
    if s['power_state'] == 'poweredOff':
        warnings.append("VM is powered off; Zerto initial sync may require a running VM depending on config")

    hw_int = _hw_version_int(s.get('hardware_version') or '')
    if hw_int is not None and hw_int < min_hw_version:
        warnings.append(f"Hardware version {s['hardware_version']} is below recommended floor (vmx-{min_hw_version})")

    if s['firmware'] == 'bios':
        # Most modern KVM hosts support both, but it's worth flagging.
        warnings.append("VM uses legacy BIOS firmware; confirm KVM target supports BIOS boot")

    return {
        'eligible': len(blockers) == 0,
        'blockers': blockers,
        'warnings': warnings,
    }


def check_migration_eligibility(
    vm_name: str,
    min_hw_version: int = DEFAULT_MIN_HW_VERSION,
    instance: Optional[str] = None,
) -> str:
    """Apply hard-blocker rules for VMware->KVM/HVM migration. Returns a
    multi-line summary including eligible bool, blockers list, and warnings."""
    si = connection.get_service_instance(instance)
    if not si:
        return "Error: Could not connect to vCenter"

    try:
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        vm = None
        try:
            for v in container.view:
                if v.name == vm_name:
                    vm = v
                    break
        finally:
            container.Destroy()

        if not vm:
            return f"VM '{vm_name}' not found"

        signals = _gather_signals(vm)
        verdict = _apply_rules(signals, min_hw_version)

        lines = [
            f"Migration eligibility for '{vm_name}':",
            f"- Eligible: {verdict['eligible']}",
            f"- Power State: {signals['power_state']}",
            f"- Guest OS: {signals['guest_id']}",
            f"- Hardware Version: {signals['hardware_version']}",
            f"- Firmware: {signals['firmware']} (Secure Boot: {signals['secure_boot']})",
            f"- VMware Tools: {signals['tools_status']}",
        ]

        if verdict['blockers']:
            lines.append("\nHard blockers:")
            for b in verdict['blockers']:
                lines.append(f"  - {b}")
        else:
            lines.append("\nHard blockers: none")

        if verdict['warnings']:
            lines.append("\nWarnings:")
            for w in verdict['warnings']:
                lines.append(f"  - {w}")
        else:
            lines.append("\nWarnings: none")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


def check_migration_eligibility_bulk(
    instance: Optional[str] = None,
    min_hw_version: int = DEFAULT_MIN_HW_VERSION,
    only_ineligible: bool = False,
) -> str:
    """Apply migration rules across every VM in the targeted vCenter. Returns
    a one-line-per-VM table for fleet-wide readiness reporting."""
    si = connection.get_service_instance(instance)
    if not si:
        return "Error: Could not connect to vCenter"

    try:
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )

        rows = []
        eligible = 0
        blocked = 0
        try:
            for vm in container.view:
                if vm.config and vm.config.template:
                    continue
                try:
                    signals = _gather_signals(vm)
                    verdict = _apply_rules(signals, min_hw_version)
                except Exception as e:
                    rows.append((vm.name, False, [f"error reading VM: {e}"], []))
                    blocked += 1
                    continue

                if verdict['eligible']:
                    eligible += 1
                else:
                    blocked += 1
                rows.append((vm.name, verdict['eligible'], verdict['blockers'], verdict['warnings']))
        finally:
            container.Destroy()

        if only_ineligible:
            rows = [r for r in rows if not r[1]]

        if not rows:
            return "No VMs matched."

        lines = [
            f"Fleet eligibility (instance={instance or 'default'}):",
            f"- Eligible: {eligible}    Blocked: {blocked}    Total: {eligible + blocked}",
            "",
        ]
        for name, ok, blockers, warnings in sorted(rows, key=lambda r: (r[1], r[0])):
            status = "OK     " if ok else "BLOCKED"
            lines.append(f"{status}  {name}")
            for b in blockers:
                lines.append(f"           blocker: {b}")
            for w in warnings:
                lines.append(f"           warning: {w}")
        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"
