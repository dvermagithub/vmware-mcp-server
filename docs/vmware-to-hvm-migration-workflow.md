# VMware to HVM Migration Workflow

Production workflow for migrating a VMware-hosted VM to a KVM/HVM target
using:

- **vmware-mcp-server** for vCenter discovery, eligibility checks,
  in-guest script execution, and Content Library ISO management.
- **ZertoMCP** for VPG management, replication, checkpoints, test
  failovers, and the actual move.
- **Morpheus MCP** for post-move agent onboarding on the KVM-hosted VM.

The design principle is **maximum protection with rollback at every
stage**. We never apply driver injection to production until the same
prep has been proven safe on a sandboxed copy, and we never commit a
move until a test failover proves the VM boots cleanly on KVM.

---

## Pre-conditions

The following must already be in place before this workflow runs:

1. The candidate VM is **already replicating from VMware to HVM**
   via an existing Zerto VPG. This pre-existing replication is the
   foundation; the workflow does not create it.
2. `.env` for vmware-mcp-server has guest credentials:
   - `GUEST_USERNAME_WINDOWS` / `GUEST_PASSWORD_WINDOWS` — local
     Administrator on Windows VMs.
   - `GUEST_USERNAME_LINUX` / `GUEST_PASSWORD_LINUX` — the `zerto`
     service account on Linux VMs (NOT root).
3. Linux candidate VMs have the `zerto` service account AND
   `/etc/sudoers.d/zerto-migration-prep` installed for NOPASSWD sudo
   on `prep-linux.sh`. See
   [`docs/linux-sudoer-setup.md`](https://github.com/dvermagithub/zerto-hvm-migration-prep/blob/master/docs/linux-sudoer-setup.md)
   in the prep repo for the sudoers contents and a sample Ansible
   task. If a VM doesn't have this and an admin can't add it,
   migration of that VM is blocked at Stage 4.
4. `report_dir` is set to a stable local path on the MCP server (your
   workstation or wherever vmware-mcp-server runs) so per-VM JSON
   summaries accumulate (e.g. `C:\reports\zerto-prep`).
5. For Windows VMs: a virtio-win ISO is available in a vCenter Content
   Library, accessible by `mount_content_library_iso` (e.g. item name
   `virtio-win-0.1.285`).
6. ZertoMCP and Morpheus MCP are configured in
   `claude_desktop_config.json` and reachable.
7. The Zerto **failover-test network** is isolated from production
   (different portgroup / test VLAN). Loopback failover-test would
   otherwise collide with the live VM's hostname / IP / MAC on AD/DNS.

---

## Stage 0 — Eligibility check

Confirm the VM can actually be migrated to KVM/HVM before doing any
prep work.

| Step | Tool | Notes |
|---|---|---|
| 0.1 | `vmware-mcp-server.get_vm_details` | Surfaces eligibility signals |
| 0.2 | `vmware-mcp-server.check_migration_eligibility` | Applies hard-blocker rules |

If `eligible == false`, stop. Resolve the blocker (decrypt vSphere VM
encryption, detach vTPM, consolidate snapshots, remove physical-mode
RDMs, etc.) and re-run from Stage 0.

---

## Stage 1 — Create loopback VPG

The loopback VPG (vSphere → same vSphere) is our prep-validation
sandbox. We use it to prove the prep scripts won't break the VM
before we apply them to production.

| Step | Tool | Notes |
|---|---|---|
| 1.1 | `ZertoMCP.create_vpg_settings` | Loopback target = source vCenter |
| 1.2 | `ZertoMCP.add_vm_to_vpg_settings` | Add the candidate VM |
| 1.3 | `ZertoMCP.update_vpg_settings_vm_nic` | Re-IP rules so the FOT bring-up doesn't collide with prod |
| 1.4 | `ZertoMCP.commit_vpg_settings` | Triggers initial sync |

---

## Stage 2 — Wait for loopback ready

Both replication health AND the first checkpoint are required before
we can do a meaningful failover-test.

| Step | Tool | Notes |
|---|---|---|
| 2.1 | `ZertoMCP.monitor_vm_replication_status` | Wait until replication is healthy / synced |
| 2.2 | `ZertoMCP.core_vpgs_get_checkpoints` | Confirm at least one checkpoint exists |

Do not proceed to Stage 3 until both signals are positive.

---

## Stage 3 — Failover-test on the loopback (sandbox bring-up)

Bring up a non-prod copy of the VM in vSphere via Zerto FOT. The live
production VM is untouched; this copy is the one we'll inject the prep
into.

| Step | Tool | Notes |
|---|---|---|
| 3.1 | `ZertoMCP.core_vpgs_get_checkpoints` | Get the latest checkpoint |
| 3.2 | `ZertoMCP.core_vpgs_failover_test` | Pass the loopback VPG ID + latest checkpoint |
| 3.3 | `vmware-mcp-server.get_vm_details` | Poll until `vmware_tools == guestToolsRunning` |

---

## Stage 4 — Inject prep into the sandbox copy

OS-specific. Linux: ship the script and run. Windows: mount the
virtio ISO from Content Library first, then ship the script and run
with `-IsoDrive`.

### Linux path

| Step | Tool | Notes |
|---|---|---|
| 4L.1 | `vmware-mcp-server.run_in_guest_via_vix` | `script_path` = path to `prep-linux.sh` on MCP server filesystem; `report_dir` set; no args needed for full run |
| 4L.2 | Verify | Tool returns the JSON summary; confirm `passed: true` |

### Windows path

| Step | Tool | Notes |
|---|---|---|
| 4W.1 | `vmware-mcp-server.list_content_library_isos` | Confirm virtio-win ISO is visible (one-time per session) |
| 4W.2 | `vmware-mcp-server.mount_content_library_iso` | Pass VM name + item name; ISO appears as a CD/DVD in the guest |
| 4W.3 | `vmware-mcp-server.run_in_guest_via_vix` | `script_path` = path to `prep-windows.ps1` on MCP server filesystem; `args="-IsoDrive D:"` (or whatever drive letter the mount used); `report_dir` set |
| 4W.4 | `vmware-mcp-server.unmount_iso_from_vm` | Clean up the CD device |
| 4W.5 | Verify | Tool returns the JSON summary; confirm `Passed: true` and all eight checks `true` |

The prep scripts are non-destructive: no removals, no reboots. They
stage drivers / install agent packages so the VM is ready to boot on
KVM after a Zerto move.

---

## Stage 5 — Stop the loopback FOT

Sandbox is no longer needed. The prep proved out on a real copy of this
exact VM.

| Step | Tool | Notes |
|---|---|---|
| 5.1 | `ZertoMCP.core_vpgs_stop_test` | Sandbox VM destroyed |

If Stage 4 verification FAILED (script errors, JSON `passed: false`,
guest doesn't come back from a script-induced state change): also call
`stop_test`, but do NOT proceed to Stage 6. Iterate on the prep script,
re-run Stage 3 and 4 against a fresh sandbox.

The loopback VPG itself stays in place — it's our prep-rollback
insurance for Stage 6.

---

## Stage 6 — Apply prep to production VM

Same script-injection flow as Stage 4, but against the live production
VM. The loopback VPG running in the background is what protects us if
this somehow breaks the VM (Zerto checkpoint restore from the loopback
is the recovery path).

### Linux path

| Step | Tool | Notes |
|---|---|---|
| 6L.1 | `vmware-mcp-server.run_in_guest_via_vix` | Same as Stage 4L.1 but against the live VM name |
| 6L.2 | Verify | JSON summary must show `passed: true` |

### Windows path

| Step | Tool | Notes |
|---|---|---|
| 6W.1 | `vmware-mcp-server.mount_content_library_iso` | Mount the same virtio-win ISO to the production VM |
| 6W.2 | `vmware-mcp-server.run_in_guest_via_vix` | Same as Stage 4W.3 but against the live VM |
| 6W.3 | `vmware-mcp-server.unmount_iso_from_vm` | Clean up |
| 6W.4 | Verify | JSON summary must show `Passed: true` |

Stage 6 does NOT reboot the production VM. The original VMware boot
path is intact; staged drivers will only bind on first boot under KVM.

**Rollback if verification fails:** restore from the latest loopback
VPG checkpoint (taken before Stage 6 started). Do not proceed to
Stage 7 until prod is confirmed healthy.

---

## Stage 7 — Failover-test on the HVM VPG (KVM bring-up)

The actual moment of truth. We bring up a copy of the prepped VM on
the KVM/HVM target. If the staged drivers work, the VM boots clean.
If they don't, we see a bluescreen / kernel panic / boot loop here,
where it's recoverable.

| Step | Tool | Notes |
|---|---|---|
| 7.1 | `ZertoMCP.core_vpgs_get_checkpoints` | Latest checkpoint of the **VMware→HVM** VPG |
| 7.2 | `ZertoMCP.core_vpgs_failover_test` | FOT against the HVM VPG |
| 7.3 | Confirm boot | Out-of-band: ping, KVM console, Morpheus discovery picking up the VM. Look for normal services running, not a recovery prompt or BSOD |

**If Stage 7 fails:** stop_test, do NOT proceed. The loopback VPG is
still in place — a checkpoint restore on prod (via the loopback) is
your rollback path. Iterate on the prep, re-test from Stage 6.

---

## Stage 8 — Cleanup before the live move

Two cleanups, both required before `core_vpgs_move`:

| Step | Tool | Notes |
|---|---|---|
| 8.1 | `ZertoMCP.core_vpgs_stop_test` | Stop the HVM FOT from Stage 7 |
| 8.2 | `ZertoMCP.core_vpgs_delete` (loopback VPG) | The loopback VPG is no longer protecting anything we still need; the prod→HVM VPG carries forward. Loopback would also block the live move (a VM cannot be source in two VPGs at the same time when one is being moved). |

After Stage 8: only the production VMware→HVM VPG remains. Verify with
`core_vpgs_list`.

---

## Stage 9 — Live move to HVM

`core_vpgs_move` shuts the source VM down on VMware and brings the
recovery VM up on KVM. The move is **not committed yet** — rollback is
still possible.

| Step | Tool | Notes |
|---|---|---|
| 9.1 | `ZertoMCP.core_vpgs_get_checkpoints` | Latest checkpoint of the HVM VPG |
| 9.2 | `ZertoMCP.core_vpgs_move` | Source down, recovery up on KVM |
| 9.3 | Confirm reachability | Out-of-band: ping, console, Morpheus discovery |

**If Stage 9 produces an unhealthy VM:** `core_vpgs_move_rollback`
brings the source VM back up on VMware. Source-side state from before
the move is preserved.

---

## Stage 10 — Morpheus onboarding

vmware-mcp-server cannot reach this VM anymore — it's no longer in
vCenter. From here, work goes through Morpheus.

> **Tool-loading note:** the Morpheus MCP currently has the agent
> action tools loaded (`install_server_agent`, `make_server_managed`,
> `servers_upgrade`) but the server-discovery tools
> (`list_servers`, `get_server`, `update_server`) may need to be
> loaded via tool_search at session start.

| Step | Tool | Notes |
|---|---|---|
| 10.1 | `morpheus.list_instances` (or `list_servers` if loaded) | Find the moved VM's server ID once Morpheus discovery picks it up |
| 10.2 | `morpheus.make_server_managed` with `installAgent: true` | Onboard + install agent in one call (brownfield path) |
| 10.3 | `morpheus.list_instances` (or `get_server`) | Verify `agentInstalled: true` and recent heartbeat |
| 10.4 | Uninstall VMware Tools | Run via Morpheus's command-execution feature (or manually). Windows: `Get-Package "VMware Tools" \| Uninstall-Package -Force`. Linux: `apt remove open-vm-tools` / `dnf remove open-vm-tools`. **Order matters: install Morpheus agent FIRST, uninstall VMware Tools SECOND.** Reversing the order leaves no remote management channel between the two steps. |

If Stage 10 fails (agent install fails, host won't register):
`core_vpgs_move_rollback` is still available. Window for rollback
closes at Stage 11.

---

## Stage 11 — Commit the move

Final step. After commit, the source VM is decommissioned and there
is **no rollback**. Only commit once the VM is fully validated on KVM
with the Morpheus agent reporting healthy.

| Step | Tool | Notes |
|---|---|---|
| 11.1 | `ZertoMCP.core_vpgs_move_commit` | Source destroyed; recovery VM is now the source-of-truth |

---

## Rollback summary

| Failure point | Rollback action | Result |
|---|---|---|
| Stage 0 (eligibility) | N/A | VM is not migratable; resolve blocker, re-run |
| Stages 1–2 (loopback setup) | `core_vpgs_delete` on the loopback | Pre-prep state, no damage |
| Stages 3–5 (sandbox) | `core_vpgs_stop_test` | Sandbox destroyed, prod and replication untouched |
| Stage 6 (prod prep) | Restore prod from latest loopback checkpoint | Prod rolled back to pre-prep state |
| Stages 7–8 (HVM FOT) | `core_vpgs_stop_test` then re-evaluate | KVM sandbox destroyed, prod untouched |
| Stage 9 (live move) | `core_vpgs_move_rollback` | Source VM brought back up on VMware |
| Stage 10 (Morpheus onboarding) | `core_vpgs_move_rollback` | Source brought back; Morpheus side discarded |
| Stage 11 (after commit) | **None** | Point of no return |

---

## Operator prompt

Paste this into Claude Desktop at the start of the session. Replace
`<VM_NAME>` and `<HVM_VPG>` with your environment values.

```
I'm running the VMware-to-HVM migration workflow for VM <VM_NAME>.
The prod-to-HVM VPG is <HVM_VPG>. The virtio-win ISO is in the vCenter
Content Library as item virtio-win-0.1.285.

Follow the workflow strictly, stage by stage. At each stage:
1. Briefly state what this stage is protecting against (one or two sentences).
2. Run the tools.
3. Interpret each result before moving on -- not just "succeeded" but
   what it means for whether the migration will work.
4. At each stage gate, summarize what you learned, state your decision
   to proceed or stop, and wait for my explicit "go" before continuing.

Optimize for me understanding your reasoning, not for terseness.

Pre-conditions confirmed:
- VM <VM_NAME> already replicates from VMware to HVM via VPG <HVM_VPG>.
- Guest credentials are in .env.
- Failover-test network is isolated.

Begin at Stage 0.
```

---

## Operational notes

- **First batch of migrations should never skip Stage 1–5.** Driver
  injection on production without sandbox validation is exactly the
  failure mode this workflow is designed to prevent. After 5–10
  successful migrations build confidence, **and only then**, you may
  consider collapsing Stages 1–5 for routine fleet rollouts of the
  same OS family.
- **Stage 7 is non-negotiable.** The KVM-side failover-test is the
  only stage that proves the prepped VM actually boots on the target
  hypervisor. Skipping it means discovering the bluescreen during the
  live move instead of during a recoverable test.
- **Conservative time gap between Stage 9 and Stage 11.** A 24-hour
  soak window between the live move and the commit is standard
  practice for non-trivial workloads. Same-session commit is only
  appropriate for stateless services where rollback isn't valuable.
- **The loopback VPG is the rollback insurance for Stage 6.** Do not
  delete it before Stage 8.
- **Cumulative effect of Stages 3 + 7:** the prep is validated twice —
  once on vSphere hardware (Stage 3, proves the script doesn't break
  the VM) and once on KVM hardware (Stage 7, proves the staged drivers
  actually load). Both passes are cheap; both protect against
  different failure modes.
