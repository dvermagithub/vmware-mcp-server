# Zerto VMware -> HVM Migration Demo Workflow

End-to-end workflow for migrating a VMware-hosted VM to a KVM/HVM target
using:

- **vmware-mcp-server** for vCenter discovery, eligibility checks, and
  in-guest script execution.
- **ZertoMCP** for replication, checkpoints, test failovers, and the
  actual move.
- **Morpheus MCP** for post-move guest ops on the KVM-hosted VM
  (uninstall VMware Tools, install Morpheus agent).

The design principle is **maximum protection with rollback at every
stage**. We never touch a production VM until the same prep has been
proven safe on a sandboxed copy, and we never commit a move until a
test failover succeeds.

---

## Prerequisites

Confirm these once before running the demo:

1. The candidate VMs already replicate to a **loopback VPG** (target =
   the same vCenter as the source). This is our sandbox.
2. The Zerto **failover-test network** is isolated from production
   (different portgroup or test VLAN) so the test bring-up cannot
   collide with the live VM's hostname / IP / MAC on AD/DNS.
3. `.env` for vmware-mcp-server has guest credentials with
   Administrator (Windows) or root (Linux) inside the guest:
   - `GUEST_USERNAME_WINDOWS` / `GUEST_PASSWORD_WINDOWS`
   - `GUEST_USERNAME_LINUX` / `GUEST_PASSWORD_LINUX`
4. `report_dir` is set to a stable local path so per-VM JSON summaries
   accumulate (e.g. `C:\reports\zerto-prep`).
5. ZertoMCP and Morpheus MCP are configured in
   `claude_desktop_config.json` and reachable.

---

## Stage 0 — Pick the candidate

| What | Tool |
|---|---|
| List VMs | `vmware-mcp-server.list_vms` |
| Get VM details + eligibility signals | `vmware-mcp-server.get_vm_details` |
| Apply hard-blocker rules | `vmware-mcp-server.check_migration_eligibility` |

If `eligible == false`, stop. The demo is over for this VM until the
blocker is resolved (decrypt, detach vTPM, consolidate snapshots, etc.).

---

## Stage 1 — Sandbox validation on the loopback VPG

We bring up a non-prod copy of the VM via Zerto test failover, inject
prep scripts there, and reboot it to confirm it doesn't bluescreen.
Nothing touches production yet.

| Step | Tool | Notes |
|---|---|---|
| 1.1 Find the loopback VPG | `ZertoMCP.core_vpgs_list` | Filter by name or by recovery site = source site |
| 1.2 Pick the **latest checkpoint** | `ZertoMCP.core_vpgs_get_checkpoints` | Sort by timestamp desc, take `[0]` |
| 1.3 Start test failover | `ZertoMCP.core_vpgs_failover_test` | Pass the VPG ID + checkpoint ID |
| 1.4 Wait for the test VM to be reachable | `vmware-mcp-server.get_vm_details` | Poll until `vmware_tools == guestToolsRunning` |
| 1.5 Inject the prep script (Windows or Linux) | `vmware-mcp-server.run_in_guest_via_vix` | Use the **test VM's name** (Zerto names it with a suffix), pass `script_path` to your fork's `prep-windows.ps1` or `prep-linux.sh`, set `report_dir` |
| 1.6 Reboot the test VM | `vmware-mcp-server.power_off_vm` then `power_on_vm` | The prep scripts deliberately do NOT reboot |
| 1.7 Re-verify post-reboot | `vmware-mcp-server.run_in_guest_via_vix` with `args="-CheckOnly"` (Windows) or `args="--check"` (Linux) | Confirms the VM came back up; pulls the JSON summary |
| 1.8 If FAILED (bluescreen, won't boot, verification fails): | `ZertoMCP.core_vpgs_stop_test` | Roll back. Iterate on the prep script. Do not proceed. |
| 1.9 If PASSED: | `ZertoMCP.core_vpgs_stop_test` | Roll back the sandbox; we now know the prep is safe |

**Decision gate:** prep proven safe on a real copy of this exact VM,
booted on KVM-equivalent virtual hardware. Do not skip this stage even
if you ran the script in dev — the production VM may have something the
dev VM didn't.

---

## Stage 2 — Production prep

Now we apply the same proven prep to the live VM. Zerto is continuously
checkpointing in the background, so we always have a rollback point —
no explicit tagging step needed; we just remember the latest checkpoint
ID from before the prep.

| Step | Tool | Notes |
|---|---|---|
| 2.1 Record the **latest checkpoint** before prep | `ZertoMCP.core_vpgs_get_checkpoints` | Save the ID; this is your "before prep" rollback point |
| 2.2 Inject the prep script on PROD | `vmware-mcp-server.run_in_guest_via_vix` | Same script and args as Stage 1.5, but against the live VM name. `fetch_log=true` and the same `report_dir` |
| 2.3 Verify the JSON summary | Inspect the `<vm-name>-summary.json` saved to `report_dir` | `passed: true` and zero hard-blocker check failures |

**Rollback if 2.3 fails:** Zerto can restore the VM to the checkpoint
from 2.1. The prep scripts are documented as non-destructive (no
removals, no reboots), so a failure here is more likely to be "verification
flagged something" than "the VM is broken." Investigate before
rolling back.

**Note:** Stage 2 does NOT reboot prod. We keep the original VMware
boot path intact until cutover; the staged drivers only bind on first
boot under KVM.

---

## Stage 3 — Replicate to HVM

If the VM doesn't already replicate to the HVM target, set up that
replication now. If it does, skip this stage.

| Step | Tool | Notes |
|---|---|---|
| 3.1 Check existing VPGs | `ZertoMCP.core_vpgs_list` | Look for one with this VM and recovery site = HVM cluster |
| 3.2 If absent: create VPG settings | `ZertoMCP.create_vpg_settings` | |
| 3.3 Add the VM | `ZertoMCP.add_vm_to_vpg_settings` | |
| 3.4 Configure NICs / re-IP for the HVM target network | `ZertoMCP.update_vpg_settings_vm_nic` | |
| 3.5 Commit the VPG | `ZertoMCP.commit_vpg_settings` | Triggers initial sync |
| 3.6 Watch the initial sync | `ZertoMCP.monitor_vm_replication_status` | Wait until status is healthy / synced |

---

## Stage 4 — Test failover to HVM

Same idea as Stage 1 but against the HVM target. Bring up a non-prod
copy on KVM, confirm it boots and binds VirtIO drivers correctly. The
VM is on KVM hardware now, so this is the real proof point.

| Step | Tool | Notes |
|---|---|---|
| 4.1 Pick the latest HVM-VPG checkpoint | `ZertoMCP.core_vpgs_get_checkpoints` | |
| 4.2 Test failover | `ZertoMCP.core_vpgs_failover_test` | |
| 4.3 Wait for the test VM to boot on KVM | (out-of-band: ping, console, Morpheus instance discovery) | This is the boot-on-KVM moment of truth |
| 4.4 If it bluescreens / won't boot: | `ZertoMCP.core_vpgs_stop_test` | Roll back. Re-investigate the prep. Stage 2 may have missed something Stage 1 didn't catch (rare, since both used the same prep). |
| 4.5 If it boots clean: | `ZertoMCP.core_vpgs_stop_test` | Roll back the test; the real move is next |

---

## Stage 5 — Live move to HVM

This is the real cutover. Zerto's `move` operation does failover + sync
of the source side; the source VM is shut down and the recovery VM is
brought up. The move is **not committed yet** — we still have rollback.

| Step | Tool | Notes |
|---|---|---|
| 5.1 Pick the latest checkpoint | `ZertoMCP.core_vpgs_get_checkpoints` | |
| 5.2 Initiate move | `ZertoMCP.core_vpgs_move` | Source is shut down, recovery VM comes up on KVM |
| 5.3 Wait for the moved VM to be reachable on KVM | (out-of-band: Morpheus instance discovery, ping, console) | |
| 5.4 If the moved VM is broken: | `ZertoMCP.core_vpgs_move_rollback` | Brings the source VM back up; recovery side is discarded. Done. |
| 5.5 If the moved VM is healthy: | continue to Stage 6 | Do NOT commit yet |

---

## Stage 6 — Post-move guest ops on KVM

VMware Tools is irrelevant on a KVM host (qemu-guest-agent was installed
by the prep script). Onboard the VM into Morpheus so it's managed under
the new platform.

The vmware-mcp-server cannot reach this VM anymore — it's no longer in
vCenter. From here, work goes through Morpheus.

> **Tool-loading note:** the Morpheus MCP currently has the agent action
> tools loaded (`install_server_agent`, `make_server_managed`,
> `servers_upgrade`) but **not** the server-discovery tools
> (`list_servers`, `get_server`, `update_server`). The action tools need
> a server ID. Two ways to get one:
>
> - Use `morpheus.list_instances` and read the server ID out of
>   `instance.servers[]`. Works for VMs that have already been
>   onboarded as Morpheus instances.
> - For brownfield discovery (a VM that just appeared on the KVM cluster
>   from a Zerto move), load the missing discovery tools first via the
>   Morpheus MCP's tool_search. One-line ask in Claude Desktop:
>   *"Load the Morpheus list_servers and get_server tools."*

| Step | Tool | Notes |
|---|---|---|
| 6.1 Find the moved VM's server ID | `morpheus.list_instances` (current setup) or `morpheus.list_servers` (after loading discovery tools) | The moved VM should appear under the HVM cloud once Morpheus's discovery cycle picks it up |
| 6.2 Convert to managed + install agent in one step | `morpheus.make_server_managed` with `installAgent: true` | Best path for a brownfield VM that Morpheus just discovered. This is the single tool that handles "this is now my VM, install the agent." |
| 6.3 (alternative if already managed) Install agent | `morpheus.install_server_agent` | Use only if the server is already in `managed=true` state but missing the agent |
| 6.4 Verify agent is reporting | `morpheus.list_instances` (or `get_server` once loaded) | Look for `agentInstalled: true` and recent heartbeat |
| 6.5 Uninstall VMware Tools | (no Morpheus tool today; run via the agent's command-execution feature once loaded, or do it in-OS) | Windows: `Get-Package "VMware Tools" \| Uninstall-Package -Force`. Linux: `apt remove open-vm-tools` / `dnf remove open-vm-tools`. **Order matters: install Morpheus agent FIRST, uninstall VMware Tools SECOND** — otherwise you have no remote management channel between the two steps. |

If anything in Stage 6 goes badly (agent install fails, host won't
register), this is still recoverable: `core_vpgs_move_rollback` from
Stage 5.4 will bring the original VMware VM back. The window for
rollback closes at Stage 7.

---

## Stage 7 — Commit the move

Final step. After commit, the source VM is decommissioned and there is
no rollback. Only commit once the VM is fully validated on KVM with the
Morpheus agent reporting healthy.

| Step | Tool | Notes |
|---|---|---|
| 7.1 Final commit | `ZertoMCP.core_vpgs_move_commit` | Source VM is destroyed; recovery VM is now the source-of-truth |

---

## Rollback summary (reverse stage order)

| If you fail at... | Roll back via | Result |
|---|---|---|
| Stage 1 (sandbox) | `core_vpgs_stop_test` | Sandbox VM destroyed, prod untouched |
| Stage 2 (prod prep) | Zerto checkpoint restore (latest pre-prep checkpoint from 2.1) | Prod VM rolled back to pre-prep state |
| Stage 3 (HVM replication) | `core_vpgs_delete` on the HVM VPG | Replication removed; prod VM still on VMware unchanged |
| Stage 4 (test failover to HVM) | `core_vpgs_stop_test` | Test VM destroyed, prod and replication untouched |
| Stage 5 (live move) | `core_vpgs_move_rollback` | Source VM brought back up on VMware |
| Stage 6 (post-move guest ops) | `core_vpgs_move_rollback` | Source VM brought back up; Morpheus side discarded |
| Stage 7 (after commit) | **None** | Point of no return |

---

## Demo prompt template

Paste this into Claude Desktop when running the demo. Replace
`<VM_NAME>`, `<LOOPBACK_VPG>`, `<HVM_VPG>` with your environment values.

```
Migrate VM <VM_NAME> from VMware to HVM using the following workflow,
stopping for confirmation before each stage gate:

Stage 0: Run check_migration_eligibility on <VM_NAME>. Stop if not
eligible.

Stage 1: Sandbox validation on loopback VPG <LOOPBACK_VPG>.
- Get latest checkpoint, test failover, inject prep script, reboot,
  re-verify with --check, stop_test on success or failure.

Stage 2: Production prep.
- Record latest checkpoint, run_in_guest_via_vix on <VM_NAME>,
  verify the JSON summary in report_dir.

Stage 3: Confirm or create HVM replication for <VM_NAME>.

Stage 4: Test failover to HVM via <HVM_VPG>, confirm boot, stop_test.

Stage 5: Live move via core_vpgs_move on <HVM_VPG>. Wait for
confirmation that the moved VM is healthy on KVM.

Stage 6: Use Morpheus on the moved VM:
- Find its server ID via list_instances (or list_servers if loaded).
- make_server_managed with installAgent=true to onboard + install agent.
- Verify agent is heartbeating.
- Then uninstall VMware Tools (in that order, never the reverse).

Stage 7: Only on my explicit confirmation, run core_vpgs_move_commit
to finalize the migration.
```

---

## Notes on safety

- **Always** run Stage 1 before Stage 2. The cost of a sandbox bring-up
  is a few minutes; the cost of a prod prep that breaks something is
  hours of downtime even with rollback.
- **Always** run Stage 4 before Stage 5. The cost of a test failover is
  capacity on the HVM target for a few minutes; the cost of a botched
  live move is real downtime plus a complex recovery.
- **Never** run Stage 7 the same day as Stage 5 unless the demo is
  scripted and the operator is ready. A 24-hour soak between Stage 5
  and Stage 7 is the conservative default in real migrations.
