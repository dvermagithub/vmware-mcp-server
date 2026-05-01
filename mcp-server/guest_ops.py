#!/usr/bin/env python3
"""
Guest Operations Module for VMware MCP Server

Runs scripts inside a VMware guest using vCenter's Guest Operations API
(pyVmomi: vim.vm.guest.ProcessManager / FileManager). Replaces the
deprecated VIX SDK; the function is named `run_in_guest_via_vix` purely
for naming continuity with VIX-era tooling.

Requirements inside the guest:
  - VMware Tools running (toolsRunningStatus == 'guestToolsRunning').
  - A guest OS account with the privileges the target script needs.
    For prep-windows.ps1 -> Administrators group member.
    For prep-linux.sh   -> root, or a sudoer (script self-checks).

Requirements on vCenter:
  - The vCenter user authenticated by connection.py needs
    VirtualMachine.GuestOperations.{Execute, Modify, Query} on the VM.

Guest credential resolution order:
  1. Explicit guest_username / guest_password arguments.
  2. Per-profile env vars when guest_profile is supplied:
        GUEST_USERNAME_WINDOWS_<PROFILE> / GUEST_PASSWORD_WINDOWS_<PROFILE>
        GUEST_USERNAME_LINUX_<PROFILE>   / GUEST_PASSWORD_LINUX_<PROFILE>
  3. OS-default env vars:
        GUEST_USERNAME_WINDOWS / GUEST_PASSWORD_WINDOWS
        GUEST_USERNAME_LINUX   / GUEST_PASSWORD_LINUX

Future: AD/Kerberos via vim.vm.guest.SAMLTokenAuthentication; SSH-key paths
for Linux are out-of-band of the vSphere Guest Ops API and would require a
separate channel.
"""

import os
import sys
import time
import ssl
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Tuple

import requests
from pyVmomi import vim, vmodl

import connection


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _find_vm(service_instance, vm_name: str):
    content = service_instance.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for v in container.view:
            if v.name == vm_name:
                return v
        return None
    finally:
        container.Destroy()


def _resolve_guest_creds(
    guest_family: str,
    guest_username: Optional[str],
    guest_password: Optional[str],
    guest_profile: Optional[str],
) -> Tuple[str, str]:
    """Resolve (username, password) from explicit args, profile, or OS default."""
    if guest_username and guest_password:
        return guest_username, guest_password

    os_tag = 'WINDOWS' if guest_family == 'windowsGuest' else 'LINUX'

    if guest_profile:
        suffix = guest_profile.upper()
        u = os.getenv(f'GUEST_USERNAME_{os_tag}_{suffix}')
        p = os.getenv(f'GUEST_PASSWORD_{os_tag}_{suffix}')
        if u and p:
            return u, p

    u = os.getenv(f'GUEST_USERNAME_{os_tag}')
    p = os.getenv(f'GUEST_PASSWORD_{os_tag}')
    if u and p:
        return u, p

    raise ValueError(
        f"No guest credentials available for {os_tag.lower()} guest. "
        f"Pass guest_username/guest_password, set GUEST_USERNAME_{os_tag}/"
        f"GUEST_PASSWORD_{os_tag} (or _<PROFILE> variant) in .env."
    )


def _build_command(
    guest_family: str,
    remote_script_path: str,
    extra_args: str,
    use_sudo: bool = True,
) -> Tuple[str, str]:
    """Return (program_path, arguments) for StartProgramInGuest.

    On Linux, use_sudo=True (default) wraps the bash invocation in
    /usr/bin/sudo so the script gets root privileges via NOPASSWD sudoers
    instead of authenticating as root directly. On Windows the parameter
    is ignored -- privilege model is "user is in Administrators group",
    not per-call escalation.
    """
    if guest_family == 'windowsGuest':
        return (
            'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
            f'-NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-File "{remote_script_path}" {extra_args}'.strip()
        )
    # Linux / other POSIX. With sudo: the program path is /usr/bin/sudo and
    # /bin/bash becomes the first argument. Without sudo: invoke bash
    # directly (caller authenticated as root).
    if use_sudo:
        return (
            '/usr/bin/sudo',
            f'-n /bin/bash "{remote_script_path}" {extra_args}'.strip()
        )
    return ('/bin/bash', f'"{remote_script_path}" {extra_args}'.strip())


def _remote_temp_dir(guest_family: str) -> str:
    return 'C:\\Windows\\Temp' if guest_family == 'windowsGuest' else '/tmp'


def _remote_script_name(guest_family: str, local_name: str) -> str:
    """Strip path, force the right extension if missing."""
    base = Path(local_name).name
    if guest_family == 'windowsGuest':
        return base if base.lower().endswith('.ps1') else base + '.ps1'
    return base if base.lower().endswith('.sh') else base + '.sh'


def _put_file_to_guest(
    service_instance,
    vm,
    auth,
    local_path: str,
    remote_path: str,
    file_attrs,
    instance: Optional[str] = None,
) -> None:
    """Upload local file to remote_path inside the guest via the Guest Ops file transfer."""
    file_data = Path(local_path).read_bytes()

    fm = service_instance.content.guestOperationsManager.fileManager
    upload_url = fm.InitiateFileTransferToGuest(
        vm=vm,
        auth=auth,
        guestFilePath=remote_path,
        fileAttributes=file_attrs,
        fileSize=len(file_data),
        overwrite=True,
    )

    # vCenter returns a URL containing '*' as a placeholder for the ESXi
    # host's address. Replace with the targeted vCenter host so the upload
    # routes through vCenter (right cert / network reachability).
    vc_host = connection.get_host(instance)
    if vc_host and '://*' in upload_url:
        upload_url = upload_url.replace('://*', f'://{vc_host}')

    resp = requests.put(
        upload_url,
        data=file_data,
        verify=False,
        timeout=60,
        headers={'Content-Type': 'application/octet-stream'},
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"File upload to guest failed: HTTP {resp.status_code} {resp.text[:200]}"
        )


def _get_file_from_guest(
    service_instance,
    vm,
    auth,
    remote_path: str,
    max_bytes: int = 1_000_000,
    instance: Optional[str] = None,
) -> Optional[str]:
    """Read a small text file from the guest. Returns None if missing/error."""
    try:
        fm = service_instance.content.guestOperationsManager.fileManager
        info = fm.InitiateFileTransferFromGuest(
            vm=vm, auth=auth, guestFilePath=remote_path
        )
    except vim.fault.FileNotFound:
        return None
    except Exception:
        return None

    download_url = info.url
    vc_host = connection.get_host(instance)
    if vc_host and '://*' in download_url:
        download_url = download_url.replace('://*', f'://{vc_host}')

    try:
        resp = requests.get(download_url, verify=False, timeout=30, stream=True)
        if resp.status_code != 200:
            return None
        body = resp.raw.read(max_bytes, decode_content=True)
        return body.decode('utf-8', errors='replace')
    except Exception:
        return None


def _wait_for_exit(
    service_instance,
    vm,
    auth,
    pid: int,
    timeout_seconds: int,
) -> Tuple[Optional[int], int]:
    """Poll ListProcessesInGuest until the process exits or timeout. Returns (exit_code, elapsed_seconds)."""
    pm = service_instance.content.guestOperationsManager.processManager
    start = time.monotonic()
    while True:
        elapsed = int(time.monotonic() - start)
        if elapsed >= timeout_seconds:
            return None, elapsed

        procs = pm.ListProcessesInGuest(vm=vm, auth=auth, pids=[pid])
        if procs:
            p = procs[0]
            if p.endTime is not None:
                return int(p.exitCode) if p.exitCode is not None else None, elapsed

        time.sleep(2)


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------
def run_in_guest_via_vix(
    vm_name: str,
    script_path: str,
    args: str = "",
    guest_username: Optional[str] = None,
    guest_password: Optional[str] = None,
    guest_profile: Optional[str] = None,
    fetch_log: bool = True,
    report_dir: Optional[str] = None,
    use_sudo: bool = True,
    timeout_seconds: int = 1800,
    instance: Optional[str] = None,
) -> str:
    """
    Upload a script (.ps1 or .sh) into a guest VM and run it via the Guest
    Operations API (no SSH, no WinRM, no PSExec). Requires VMware Tools running
    in the guest and a privileged guest OS account.

    Returns a multi-line summary including exit code and (optionally) the
    Zerto-migration-prep JSON summary or the tail of the log.
    """
    if not Path(script_path).is_file():
        return f"Error: script not found: {script_path}"

    si = connection.get_service_instance(instance)
    if not si:
        return "Error: Could not connect to vCenter"

    vm = _find_vm(si, vm_name)
    if not vm:
        return f"Error: VM '{vm_name}' not found"

    # Tools precondition.
    tools_running = getattr(vm.guest, 'toolsRunningStatus', None)
    if tools_running != 'guestToolsRunning':
        return (
            f"Error: VMware Tools not running on '{vm_name}' "
            f"(toolsRunningStatus={tools_running}). Guest Operations API requires "
            f"running tools."
        )

    guest_family = getattr(vm.guest, 'guestFamily', None)
    if guest_family not in ('windowsGuest', 'linuxGuest', 'otherGuestFamily'):
        return (
            f"Error: unsupported guestFamily={guest_family!r}. "
            f"Only windowsGuest and linuxGuest are supported."
        )

    try:
        username, password = _resolve_guest_creds(
            guest_family, guest_username, guest_password, guest_profile
        )
    except ValueError as e:
        return f"Error: {e}"

    auth = vim.vm.guest.NamePasswordAuthentication(
        username=username, password=password, interactiveSession=False
    )

    # Validate creds early -- ValidateCredentialsInGuest is cheaper than a
    # failed upload and gives a clean error message if the password is wrong.
    try:
        si.content.guestOperationsManager.authManager.ValidateCredentialsInGuest(
            vm=vm, auth=auth
        )
    except vim.fault.InvalidGuestLogin:
        return f"Error: invalid guest credentials for user '{username}' on '{vm_name}'"
    except vim.fault.GuestOperationsUnavailable:
        return (
            "Error: Guest Operations are unavailable on this VM "
            "(tools may be starting; retry in a few seconds)"
        )
    except vmodl.fault.NotSupported as e:
        return f"Error: Guest Operations not supported on this VM: {e.msg}"
    except Exception as e:
        return f"Error: credential validation failed: {e}"

    # Pick a remote path and upload.
    remote_dir = _remote_temp_dir(guest_family)
    remote_name = _remote_script_name(guest_family, script_path)
    sep = '\\' if guest_family == 'windowsGuest' else '/'
    remote_script = f"{remote_dir}{sep}{remote_name}"

    if guest_family == 'windowsGuest':
        file_attrs = vim.vm.guest.FileManager.WindowsFileAttributes()
    else:
        file_attrs = vim.vm.guest.FileManager.PosixFileAttributes(permissions=0o700)

    try:
        _put_file_to_guest(si, vm, auth, script_path, remote_script, file_attrs, instance=instance)
    except Exception as e:
        return f"Error: failed to upload script to guest: {e}"

    # Build and start the process.
    program_path, arguments = _build_command(guest_family, remote_script, args, use_sudo=use_sudo)
    spec = vim.vm.guest.ProcessManager.ProgramSpec(
        programPath=program_path,
        arguments=arguments,
        workingDirectory=remote_dir,
    )
    try:
        pid = si.content.guestOperationsManager.processManager.StartProgramInGuest(
            vm=vm, auth=auth, spec=spec
        )
    except Exception as e:
        return f"Error: StartProgramInGuest failed: {e}"

    exit_code, elapsed = _wait_for_exit(si, vm, auth, pid, timeout_seconds)

    # Build the result.
    lines = [
        f"VM:           {vm_name}",
        f"Guest family: {guest_family}",
        f"Account:      {username}",
        f"Script:       {remote_script}",
        f"PID:          {pid}",
        f"Elapsed:      {elapsed}s",
    ]
    if exit_code is None:
        lines.append("Exit code:    (timeout — process still running in guest)")
    else:
        lines.append(f"Exit code:    {exit_code}")

    summary_text: Optional[str] = None
    if fetch_log:
        if guest_family == 'windowsGuest':
            summary_path = 'C:\\ProgramData\\ZertoMigrationPrep\\logs\\last-run-summary.json'
        else:
            # Read the world-readable mirror in /tmp so the unprivileged
            # guest user used by Guest Ops can fetch it. The canonical
            # /var/log copy stays 0600.
            summary_path = '/tmp/zerto-migration-prep-summary.json'

        summary_text = _get_file_from_guest(
            si, vm, auth, summary_path, instance=instance,
        )
        if summary_text:
            lines.append(f"---- {summary_path} ----")
            lines.append(summary_text.strip())
        else:
            lines.append(f"(no summary file at {summary_path})")

    if report_dir and summary_text:
        try:
            out_dir = Path(report_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            # Filename: <vm-name>-summary.json. Sanitise path separators so a
            # weird VM name can't escape report_dir.
            safe_name = vm_name.replace('/', '_').replace('\\', '_')
            out_path = out_dir / f"{safe_name}-summary.json"
            out_path.write_text(summary_text, encoding='utf-8')
            lines.append(f"Saved summary -> {out_path}")
        except Exception as e:
            lines.append(f"(failed to save summary to report_dir: {e})")

    return "\n".join(lines)
