#!/usr/bin/env python3
"""
Connection Management Module for VMware MCP Server
Handles vCenter connections and session management.

Supports multiple vCenters via per-instance environment variables:
    VCENTER_HOSTS=prod,dr,lab          # comma-separated instance names
    VCENTER_HOST_PROD=...              # per-instance host
    VCENTER_USER_PROD=...
    VCENTER_PASSWORD_PROD=...
    VCENTER_DEFAULT=prod               # optional; first in list otherwise

Backwards compatible: if VCENTER_HOSTS is not set, falls back to the legacy
single-vCenter env vars (VCENTER_HOST / VCENTER_USER / VCENTER_PASSWORD)
under the synthetic instance name 'default'.
"""

import os
import ssl
import socket
import requests
import sys
from typing import Optional, Dict, Any, List
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

# Per-instance cached service instances and REST sessions
_service_instances: Dict[str, Any] = {}
_rest_sessions: Dict[str, str] = {}


def list_instances() -> List[str]:
    """Return configured instance names."""
    raw = os.getenv('VCENTER_HOSTS', '').strip()
    if raw:
        return [n.strip() for n in raw.split(',') if n.strip()]
    # Legacy fallback: single instance named 'default'
    if os.getenv('VCENTER_HOST'):
        return ['default']
    return []


def default_instance() -> Optional[str]:
    """Return the default instance name, or None if no instances configured."""
    explicit = os.getenv('VCENTER_DEFAULT', '').strip()
    if explicit:
        return explicit
    instances = list_instances()
    return instances[0] if instances else None


def _resolve_instance(instance: Optional[str]) -> str:
    """Resolve an instance name, falling back to default. Raises ValueError if none."""
    name = instance or default_instance()
    if not name:
        raise ValueError(
            "No vCenter instances configured. Set VCENTER_HOSTS=name1,name2 "
            "with VCENTER_HOST_<NAME>/VCENTER_USER_<NAME>/VCENTER_PASSWORD_<NAME>, "
            "or set legacy VCENTER_HOST/VCENTER_USER/VCENTER_PASSWORD."
        )
    return name


def _creds_for(instance: str) -> Dict[str, Optional[str]]:
    """Look up host/user/password for an instance from env vars."""
    if instance == 'default' and not os.getenv('VCENTER_HOSTS'):
        # Legacy single-vCenter mode (accept both VCENTER_USER and VCENTER_USERNAME)
        return {
            'host': os.getenv('VCENTER_HOST'),
            'user': os.getenv('VCENTER_USER') or os.getenv('VCENTER_USERNAME'),
            'password': os.getenv('VCENTER_PASSWORD'),
        }
    suffix = instance.upper()
    return {
        'host': os.getenv(f'VCENTER_HOST_{suffix}'),
        'user': os.getenv(f'VCENTER_USER_{suffix}'),
        'password': os.getenv(f'VCENTER_PASSWORD_{suffix}'),
    }


def get_host(instance: Optional[str] = None) -> Optional[str]:
    """Get host for an instance (used by REST callers)."""
    try:
        name = _resolve_instance(instance)
    except ValueError:
        return None
    return _creds_for(name).get('host')


def connect_to_vcenter(instance: Optional[str] = None) -> bool:
    """Connect (or reuse cached connection) for an instance."""
    try:
        name = _resolve_instance(instance)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return False

    cached = _service_instances.get(name)
    if cached:
        try:
            # currentTime() forces a real round-trip and surfaces an idle-timed-out
            # session as an exception, where RetrieveContent() can return a stale
            # cached object without hitting the wire.
            cached.CurrentTime()
            return True
        except Exception:
            _service_instances.pop(name, None)

    creds = _creds_for(name)
    host, user, password = creds['host'], creds['user'], creds['password']
    if not all([host, user, password]):
        print(
            f"vCenter instance '{name}' is missing credentials "
            f"(host/user/password env vars).",
            file=sys.stderr,
        )
        return False

    try:
        socket.setdefaulttimeout(3)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS)
        context.verify_mode = ssl.CERT_NONE
        context.check_hostname = False

        si = SmartConnect(host=host, user=user, pwd=password, sslContext=context)
        _service_instances[name] = si
        return True
    except Exception as e:
        print(f"Connection error for '{name}': {e}", file=sys.stderr)
        return False


def get_service_instance(instance: Optional[str] = None):
    """Get the cached service instance for an instance, connecting if necessary."""
    if connect_to_vcenter(instance):
        name = _resolve_instance(instance)
        return _service_instances.get(name)
    return None


def _rest_session_alive(host: str, session_id: str) -> bool:
    """Cheap GET against the session endpoint — 200 if alive, 401 if expired."""
    try:
        r = requests.get(
            f"https://{host}/rest/com/vmware/cis/session",
            headers={"vmware-api-session-id": session_id},
            verify=False, timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def _create_rest_session(host: str, user: str, password: str) -> Optional[str]:
    try:
        r = requests.post(
            f"https://{host}/rest/com/vmware/cis/session",
            auth=(user, password), verify=False, timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("value")
        print(f"Failed to create REST session: {r.status_code} {r.text[:200]}",
              file=sys.stderr)
        return None
    except Exception as e:
        print(f"Session error: {e}", file=sys.stderr)
        return None


def get_vcenter_session(instance: Optional[str] = None,
                        force_refresh: bool = False) -> Optional[str]:
    """Get a vCenter REST API session id for an instance.

    Validates the cached session against vCenter on every call; if the cached
    session has expired (idle > server timeout) it is silently re-created.
    Pass `force_refresh=True` to skip the cache and re-login unconditionally
    (use this from a caller that just got a 401 to recover in one round-trip).
    """
    try:
        name = _resolve_instance(instance)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return None

    creds = _creds_for(name)
    host, user, password = creds['host'], creds['user'], creds['password']
    if not all([host, user, password]):
        return None

    if not force_refresh:
        cached = _rest_sessions.get(name)
        if cached and _rest_session_alive(host, cached):
            return cached
        if cached:
            _rest_sessions.pop(name, None)

    session_id = _create_rest_session(host, user, password)
    if session_id:
        _rest_sessions[name] = session_id
    return session_id


def invalidate_session(instance: Optional[str] = None) -> None:
    """Drop both cached SOAP and REST sessions for an instance so the next
    call re-authenticates. Callers should invoke this on a 401 from any
    REST tool, or when SOAP raises a not-authenticated fault."""
    try:
        name = _resolve_instance(instance)
    except ValueError:
        return
    _service_instances.pop(name, None)
    _rest_sessions.pop(name, None)


def disconnect_vcenter(instance: Optional[str] = None) -> None:
    """Disconnect a single instance, or all instances if instance is None."""
    targets = [instance] if instance else list(_service_instances.keys())
    for name in targets:
        try:
            name = _resolve_instance(name)
        except ValueError:
            continue
        si = _service_instances.pop(name, None)
        if si:
            try:
                Disconnect(si)
            except Exception:
                pass
        _rest_sessions.pop(name, None)


def list_vcenters_summary() -> str:
    """Human-readable summary of configured vCenters (for the list_vcenters tool)."""
    instances = list_instances()
    if not instances:
        return "No vCenter instances configured."
    default = default_instance()
    lines = [f"Configured vCenters ({len(instances)}):"]
    for name in instances:
        creds = _creds_for(name)
        host = creds.get('host') or '(missing)'
        marker = ' [default]' if name == default else ''
        connected = ' [connected]' if name in _service_instances else ''
        lines.append(f"- {name}: host={host}{marker}{connected}")
    return "\n".join(lines)
