#!/usr/bin/env python3
"""
VM Devices Module for VMware MCP Server
Handles VM device operations such as mounting/unmounting ISO images on the
virtual CD/DVD drive. Works on both powered-on and powered-off VMs.
"""

from typing import Optional, List, Dict, Any
import requests
from pyVmomi import vim
import connection


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


def _find_datastore(service_instance, datastore_name: str):
    content = service_instance.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datastore], True
    )
    try:
        for ds in container.view:
            if ds.name == datastore_name:
                return ds
        return None
    finally:
        container.Destroy()


def _find_cdrom(vm):
    for device in vm.config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualCdrom):
            return device
    return None


def _find_ide_controller(vm):
    for device in vm.config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualIDEController):
            if len(device.device) < 2:
                return device
    return None


def _wait_for_task(task):
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        pass


def mount_iso_to_vm(vm_name: str, datastore: str, iso_path: str,
                    instance: Optional[str] = None) -> str:
    """Mount an ISO image to a VM's CD/DVD drive.

    iso_path is the path inside the datastore (e.g. 'isos/ubuntu-22.04.iso').
    Works on both powered-on and powered-off VMs.
    """
    service_instance = connection.get_service_instance(instance)
    if not service_instance:
        return "Error: Could not connect to vCenter"

    try:
        vm = _find_vm(service_instance, vm_name)
        if not vm:
            return f"VM '{vm_name}' not found"

        ds = _find_datastore(service_instance, datastore)
        if not ds:
            return f"Datastore '{datastore}' not found. Use list_datastores() to see available datastores."

        normalized_path = iso_path.lstrip("/").lstrip("\\")
        file_name = f"[{datastore}] {normalized_path}"

        iso_backing = vim.vm.device.VirtualCdrom.IsoBackingInfo()
        iso_backing.fileName = file_name
        iso_backing.datastore = ds

        connect_info = vim.vm.device.VirtualDevice.ConnectInfo()
        connect_info.allowGuestControl = True
        connect_info.startConnected = True
        connect_info.connected = True

        cdrom = _find_cdrom(vm)
        device_spec = vim.vm.device.VirtualDeviceSpec()

        if cdrom:
            device_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            cdrom.backing = iso_backing
            cdrom.connectable = connect_info
            device_spec.device = cdrom
        else:
            ide = _find_ide_controller(vm)
            if not ide:
                return f"❌ No free IDE controller slot on VM '{vm_name}' to add a CD-ROM"
            device_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            new_cdrom = vim.vm.device.VirtualCdrom()
            new_cdrom.controllerKey = ide.key
            new_cdrom.unitNumber = 0 if not ide.device else (max(ide.device) + 1)
            new_cdrom.key = -1
            new_cdrom.backing = iso_backing
            new_cdrom.connectable = connect_info
            device_spec.device = new_cdrom

        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [device_spec]

        task = vm.ReconfigVM_Task(spec=config_spec)
        _wait_for_task(task)

        if task.info.state == vim.TaskInfo.State.success:
            return (
                f"✅ Mounted ISO '{file_name}' to VM '{vm_name}'"
                f" (power state: {vm.runtime.powerState})"
            )
        return f"❌ Failed to mount ISO on VM '{vm_name}': {task.info.error.msg}"

    except Exception as e:
        return f"Error: {e}"


def unmount_iso_from_vm(vm_name: str, instance: Optional[str] = None) -> str:
    """Unmount whatever ISO is currently attached to the VM's CD/DVD drive.

    Replaces the ISO backing with a client-device passthrough backing and
    marks the drive as disconnected. Works on both powered-on and powered-off
    VMs.
    """
    service_instance = connection.get_service_instance(instance)
    if not service_instance:
        return "Error: Could not connect to vCenter"

    try:
        vm = _find_vm(service_instance, vm_name)
        if not vm:
            return f"VM '{vm_name}' not found"

        cdrom = _find_cdrom(vm)
        if not cdrom:
            return f"VM '{vm_name}' has no CD/DVD drive to unmount"

        if not isinstance(cdrom.backing, vim.vm.device.VirtualCdrom.IsoBackingInfo):
            return f"VM '{vm_name}' CD/DVD drive has no ISO mounted"

        passthrough = vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo()
        passthrough.exclusive = False
        passthrough.deviceName = ""

        connect_info = vim.vm.device.VirtualDevice.ConnectInfo()
        connect_info.allowGuestControl = True
        connect_info.startConnected = False
        connect_info.connected = False

        cdrom.backing = passthrough
        cdrom.connectable = connect_info

        device_spec = vim.vm.device.VirtualDeviceSpec()
        device_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        device_spec.device = cdrom

        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = [device_spec]

        task = vm.ReconfigVM_Task(spec=config_spec)
        _wait_for_task(task)

        if task.info.state == vim.TaskInfo.State.success:
            return f"✅ Unmounted ISO from VM '{vm_name}'"
        return f"❌ Failed to unmount ISO from VM '{vm_name}': {task.info.error.msg}"

    except Exception as e:
        return f"Error: {e}"


def _cl_get(host: str, session_id: str, path: str) -> Any:
    url = f"https://{host}/api{path}"
    r = requests.get(
        url, headers={"vmware-api-session-id": session_id},
        verify=False, timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("value", body) if isinstance(body, dict) else body


def _cl_post(host: str, session_id: str, path: str, body: Optional[dict] = None) -> Any:
    url = f"https://{host}/api{path}"
    r = requests.post(
        url, headers={"vmware-api-session-id": session_id},
        json=body, verify=False, timeout=30,
    )
    r.raise_for_status()
    if not r.text:
        return None
    parsed = r.json()
    return parsed.get("value", parsed) if isinstance(parsed, dict) else parsed


def _cl_iter_iso_items(host: str, session_id: str,
                       library_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return a list of {library, library_id, item, item_id, file_name, size}
    for every ISO file in every (matching) content library."""
    library_ids = _cl_get(host, session_id, "/content/library")
    results: List[Dict[str, Any]] = []
    for lib_id in library_ids:
        lib = _cl_get(host, session_id, f"/content/library/{lib_id}")
        lib_name = lib.get("name", lib_id)
        if library_filter and lib_name != library_filter:
            continue
        item_ids = _cl_get(host, session_id, f"/content/library/item?library_id={lib_id}")
        for item_id in item_ids:
            item = _cl_get(host, session_id, f"/content/library/item/{item_id}")
            if (item.get("type") or "").lower() != "iso":
                continue
            try:
                files = _cl_get(host, session_id, f"/content/library/item/{item_id}/file")
            except Exception:
                files = []
            iso_file = next(
                (f for f in files if (f.get("name") or "").lower().endswith(".iso")),
                files[0] if files else None,
            )
            results.append({
                "library": lib_name,
                "library_id": lib_id,
                "item": item.get("name", item_id),
                "item_id": item_id,
                "file_name": (iso_file or {}).get("name"),
                "size": (iso_file or {}).get("size"),
            })
    return results


def list_content_library_isos(instance: Optional[str] = None) -> str:
    """List all ISO items across every content library on the targeted vCenter."""
    host = connection.get_host(instance)
    session_id = connection.get_vcenter_session(instance)
    if not host or not session_id:
        return "Error: Could not establish vCenter REST session"

    try:
        items = _cl_iter_iso_items(host, session_id)
    except Exception as e:
        return f"Error listing content library ISOs: {e}"

    if not items:
        return "No ISO items found in any content library."

    lines = [f"Content library ISOs ({len(items)}):"]
    for it in items:
        size_mb = f"{it['size'] / (1024 * 1024):.0f} MB" if it.get('size') else "?"
        lines.append(
            f"- [{it['library']}] {it['item']}  ({it['file_name']}, {size_mb})"
        )
    return "\n".join(lines)


def mount_content_library_iso(vm_name: str, item_name: str,
                              library_name: Optional[str] = None,
                              instance: Optional[str] = None) -> str:
    """Mount an ISO from a vCenter content library to a VM by item name.

    Uses the supported `/api/vcenter/vm/{vm}/hardware/cdrom` endpoint with an
    ISO_FILE backing whose `iso_file` is the content library item ID — vCenter
    resolves the on-disk path itself. If multiple libraries contain an item
    with the same name, pass `library_name` to disambiguate. Works on both
    powered-on and powered-off VMs.
    """
    host = connection.get_host(instance)
    session_id = connection.get_vcenter_session(instance)
    if not host or not session_id:
        return "Error: Could not establish vCenter REST session"

    try:
        items = _cl_iter_iso_items(host, session_id, library_filter=library_name)
    except Exception as e:
        return f"Error listing content library ISOs: {e}"

    matches = [it for it in items if it["item"] == item_name]
    if not matches:
        scope = f"library '{library_name}'" if library_name else "any content library"
        return f"ISO item '{item_name}' not found in {scope}. Use list_content_library_isos() to see available items."
    if len(matches) > 1:
        libs = ", ".join(sorted({m["library"] for m in matches}))
        return f"ISO item '{item_name}' found in multiple libraries ({libs}). Pass library_name to disambiguate."

    item_id = matches[0]["item_id"]

    # Resolve the actual datastore path for the library item via the storage API.
    try:
        raw_storage = _cl_get(
            host, session_id,
            f"/content/library/item/{item_id}/storage",
        )
    except Exception as e:
        return f"Error querying content library item storage: {e}"

    backings = raw_storage if isinstance(raw_storage, list) else []
    if not backings:
        return (
            f"Content library item '{item_name}' has no storage backings yet "
            f"(subscribed-but-not-cached?). Sync the library and retry. "
            f"Raw response: {raw_storage!r}"
        )

    iso_backing = next(
        (b for b in backings if (b.get("name") or "").lower().endswith(".iso")),
        backings[0],
    )

    ds_path = None
    candidate_uris: List[str] = []
    for key in ("storage_uris", "storage_uri", "uri"):
        v = iso_backing.get(key)
        if isinstance(v, list):
            candidate_uris.extend(u for u in v if u)
        elif isinstance(v, str) and v:
            candidate_uris.append(v)

    for uri in candidate_uris:
        # Strip query string (vCenter appends ?serverId=...).
        clean = uri.split("?", 1)[0]
        if clean.startswith("[") and "]" in clean:
            ds_path = clean
            break
        if clean.startswith("ds:///vmfs/volumes/"):
            tail = clean[len("ds:///vmfs/volumes/"):]
            # vCenter sometimes emits a double slash after the UUID — collapse.
            while "//" in tail:
                tail = tail.replace("//", "/")
            ds_token, _, rel = tail.partition("/")
            if rel:
                ds_path = f"[{ds_token}] {rel}"
                break

    if not ds_path:
        return (
            f"Could not parse datastore path from storage response. "
            f"Raw backing: {iso_backing!r}"
        )

    ds_token = ds_path[1:ds_path.index("]")]
    rel_path = ds_path[ds_path.index("]") + 1:].strip()

    service_instance = connection.get_service_instance(instance)
    if not service_instance:
        return "Error: Could not connect to vCenter"

    ds_name = ds_token
    if _find_datastore(service_instance, ds_token) is None:
        # ds_token is a datastore UUID, not a name — resolve via summary.url.
        content = service_instance.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datastore], True
        )
        try:
            mapped = None
            for ds in container.view:
                if ds_token in (ds.summary.url or ""):
                    mapped = ds.name
                    break
            if not mapped:
                return (
                    f"Could not map datastore token '{ds_token}' from content "
                    f"library URI to a known datastore name."
                )
            ds_name = mapped
        finally:
            container.Destroy()

    return mount_iso_to_vm(vm_name, ds_name, rel_path, instance)
