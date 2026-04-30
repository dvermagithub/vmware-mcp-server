#!/usr/bin/env python3
"""
VMware MCP Server - Main Entry Point
Clean, modular FastMCP server for VMware vCenter management.

Multi-vCenter: every tool accepts an optional `instance` parameter naming
which configured vCenter to target. When omitted, the default instance is used.
Use `list_vcenters` to discover configured instance names.
"""

from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from fastmcp import FastMCP
import vm_info
import power
import vm_creation
import monitoring
import host_info
import maintenance
import connection
import guest_ops

# Load .env from the repo root (one level up from mcp-server/)
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Create the MCP server instance
mcp = FastMCP(name="VMware MCP Server")


# Multi-vCenter Discovery
@mcp.tool()
def list_vcenters() -> str:
    """List all configured vCenter instances this MCP server can talk to. Returns the instance names to pass as the `instance` parameter on other tools, plus host and default flag."""
    return connection.list_vcenters_summary()


# VM Information Tools
@mcp.tool()
def list_vms(instance: Optional[str] = None) -> str:
    """List all VMs using fast REST API. Optionally target a specific vCenter instance."""
    return vm_info.list_vms(instance)

@mcp.tool()
def get_vm_details(vm_name: str, instance: Optional[str] = None) -> str:
    """Get detailed VM information including IP addresses and network info. Optionally target a specific vCenter instance."""
    return vm_info.get_vm_details(vm_name, instance)

@mcp.tool()
def list_templates(instance: Optional[str] = None) -> str:
    """List all available templates. Optionally target a specific vCenter instance."""
    return vm_info.list_templates(instance)

@mcp.tool()
def list_datastores(instance: Optional[str] = None) -> str:
    """List all available datastores. Optionally target a specific vCenter instance."""
    return vm_info.list_datastores(instance)

@mcp.tool()
def list_networks(instance: Optional[str] = None) -> str:
    """List all available networks. Optionally target a specific vCenter instance."""
    return vm_info.list_networks(instance)

# Power Management Tools
@mcp.tool()
def power_on_vm(vm_name: str, instance: Optional[str] = None) -> str:
    """Power on a VM by name. Optionally target a specific vCenter instance."""
    return power.power_on_vm(vm_name, instance)

@mcp.tool()
def power_off_vm(vm_name: str, instance: Optional[str] = None) -> str:
    """Power off a VM by name. Optionally target a specific vCenter instance."""
    return power.power_off_vm(vm_name, instance)

# VM Creation Tools
@mcp.tool()
def create_vm_custom(template_name: str, new_vm_name: str, ip_address: str = "192.168.1.100",
                    netmask: str = "255.255.255.0", gateway: str = "192.168.1.1",
                    memory_gb: int = 4, cpu_count: int = 2, disk_gb: int = 50,
                    network_name: str = "VM Network", datastore_name: str = "datastore1",
                    instance: Optional[str] = None) -> str:
    """Create a new VM from template with comprehensive customization (memory, CPU, disk, IP) - powered off by default. Optionally target a specific vCenter instance."""
    return vm_creation.create_vm_custom(
        template_name=template_name,
        new_vm_name=new_vm_name,
        ip_address=ip_address,
        netmask=netmask,
        gateway=gateway,
        memory_gb=memory_gb,
        cpu_count=cpu_count,
        disk_gb=disk_gb,
        network_name=network_name,
        datastore_name=datastore_name,
        instance=instance,
    )

# Host Information Tools
@mcp.tool()
def list_hosts(instance: Optional[str] = None) -> str:
    """List all physical hosts with basic information. Optionally target a specific vCenter instance."""
    return host_info.list_hosts(instance)

@mcp.tool()
def get_host_details(host_name: str, instance: Optional[str] = None) -> str:
    """Get detailed information about a specific physical host (hardware, network, storage, VMs). Optionally target a specific vCenter instance."""
    return host_info.get_host_details(host_name, instance)

@mcp.tool()
def get_host_performance_metrics(host_name: str, instance: Optional[str] = None) -> str:
    """Get detailed performance metrics for a specific host (CPU, memory, disk, network). Optionally target a specific vCenter instance."""
    return host_info.get_host_performance_metrics(host_name, instance)

@mcp.tool()
def get_host_hardware_health(host_name: str, instance: Optional[str] = None) -> str:
    """Get hardware health information for a specific host (sensors, system health). Optionally target a specific vCenter instance."""
    return host_info.get_host_hardware_health(host_name, instance)

# Monitoring Tools
@mcp.tool()
def get_vm_performance(vm_name: str, instance: Optional[str] = None) -> str:
    """Get detailed performance metrics for a specific VM (CPU, memory, disk, network). Optionally target a specific vCenter instance."""
    return monitoring.get_vm_performance(vm_name, instance)

@mcp.tool()
def get_host_performance(host_name: str = "", instance: Optional[str] = None) -> str:
    """Get performance metrics for hosts (hardware info, health status). Optionally target a specific vCenter instance."""
    if not host_name:
        return "Error: Host name is required"
    return monitoring.get_host_performance(host_name, instance)

@mcp.tool()
def list_performance_counters(instance: Optional[str] = None) -> str:
    """List all available performance counters in vCenter. Optionally target a specific vCenter instance."""
    return monitoring.list_performance_counters(instance)

@mcp.tool()
def get_vm_summary_stats(instance: Optional[str] = None) -> str:
    """Get summary statistics for all VMs (counts, resource totals). Optionally target a specific vCenter instance."""
    return monitoring.get_vm_summary_stats(instance)

# Maintenance Tools
@mcp.tool()
def get_maintenance_instructions() -> str:
    """Get the maintenance instructions from the maintenance-vmware.md file."""
    return maintenance.read_maintenance_instructions()

@mcp.tool()
def get_maintenance_plan(instance: Optional[str] = None) -> str:
    """Get a maintenance plan showing what VMs will be affected and the instructions. Optionally target a specific vCenter instance."""
    return maintenance.get_maintenance_plan(instance)

@mcp.tool()
def execute_power_down_sequence(instance: Optional[str] = None) -> str:
    """Execute the power-down sequence based on maintenance instructions. Optionally target a specific vCenter instance."""
    return maintenance.execute_power_down_sequence(instance)

@mcp.tool()
def execute_power_up_sequence(instance: Optional[str] = None) -> str:
    """Execute the power-up sequence based on maintenance instructions. Optionally target a specific vCenter instance."""
    return maintenance.execute_power_up_sequence(instance)

# Guest Operations Tools
@mcp.tool()
def run_in_guest_via_vix(
    vm_name: str,
    script_path: str,
    args: str = "",
    guest_username: Optional[str] = None,
    guest_password: Optional[str] = None,
    guest_profile: Optional[str] = None,
    fetch_log: bool = True,
    report_dir: Optional[str] = None,
    timeout_seconds: int = 1800,
    instance: Optional[str] = None,
) -> str:
    """Upload and run a local .ps1 or .sh script inside a VMware guest via the
    vCenter Guest Operations API (no SSH/WinRM/PSExec). Requires VMware Tools
    running in the guest plus a privileged guest OS account (Administrator on
    Windows, root/sudoer on Linux). Guest creds are taken from
    guest_username/guest_password if provided, otherwise from .env
    (GUEST_USERNAME_WINDOWS/_LINUX or _<PROFILE> variants). When fetch_log is
    true, pulls back last-run-summary.json from the guest
    (C:\\ProgramData\\ZertoMigrationPrep\\logs\\ on Windows,
    /var/log/zerto-migration-prep/ on Linux) and embeds it in the response.
    When report_dir is set, also saves the summary to
    <report_dir>/<vm-name>-summary.json on the MCP server's filesystem so
    a fleet run can accumulate one JSON per host for downstream reporting.
    Optionally target a specific vCenter instance."""
    return guest_ops.run_in_guest_via_vix(
        vm_name=vm_name,
        script_path=script_path,
        args=args,
        guest_username=guest_username,
        guest_password=guest_password,
        guest_profile=guest_profile,
        fetch_log=fetch_log,
        report_dir=report_dir,
        timeout_seconds=timeout_seconds,
        instance=instance,
    )

if __name__ == "__main__":
    import os

    # Get transport mode from environment variable, default to stdio
    transport_mode = (os.getenv('MCP_TRANSPORT') or 'stdio').lower()

    if transport_mode == 'sse':
        # SSE mode for web clients like n8n
        host = os.getenv('MCP_HOST', '127.0.0.1')
        port = int(os.getenv('MCP_PORT', '8000'))
        print(f"Starting VMware MCP Server in SSE mode on {host}:{port}")
        mcp.run(transport="sse", host=host, port=port)
    elif transport_mode == 'http':
        # HTTP mode for web deployments
        host = os.getenv('MCP_HOST', '127.0.0.1')
        port = int(os.getenv('MCP_PORT', '8000'))
        path = os.getenv('MCP_PATH') or '/mcp'
        print(f"Starting VMware MCP Server in HTTP mode on {host}:{port}{path}")
        mcp.run(transport="http", host=host, port=port, path=path)
    else:
        # STDIO mode (default) for local tools like Goose
        print("Starting VMware MCP Server in STDIO mode")
        mcp.run()
