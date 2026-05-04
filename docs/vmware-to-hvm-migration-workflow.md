# VMware to HVM Migration Workflow

Production workflow for migrating a VMware-hosted VM to a KVM/HVM target
using:

- **vmware-mcp-server** for vCenter discovery, eligibility checks,
  in-guest script execution, and Content Library ISO management.
- **ZertoMCP** for VPG management, replication, checkpoints, test
  failovers, and the actual move.
- **Morpheus MCP** for post-move agent onboarding on the KVM-hosted VM.
- **Cohesity** (or your existing backup tool) for rollback insurance
  before we touch the source VM.

The design principle is **maximum protection with rollback at every
stage**. Each stage either has a documented rollback path, or proves
something we need before we proceed. We never commit a move until a
test failover proves the VM boots cleanly on KVM.

---

## Pre-conditions

The following must already be in place before this workflow runs:

1. The candidate VM is **already replicating from VMware to HVM** via
   an existing Zerto VPG. The workflow validates and uses this
   replication; it does not create it.
2. The candidate VM has **a recent Cohesity backup** (or equivalent
   point-in-time backup that can restore the full VM). This is the
   rollback path if something in the prep step goes wrong on the
   source VM.
3. `.env` for vmware-mcp-server has guest credentials:
   - `GUEST_USERNAME_WINDOWS` / `GUEST_PASSWORD_WINDOWS` — local
     Administrator on Windows VMs.
   - `GUEST_USERNAME_LINUX` / `GUEST_PASSWORD_LINUX` — the `zerto`
     service account on Linux VMs (NOT root).
4. Linux candidate VMs have the `zerto` service account AND
   `/etc/sudoers.d/zerto-migration-prep` installed for NOPASSWD sudo
   on `prep-linux.sh`. See
   [`docs/linux-sudoer-setup.md`](https://github.com/dvermagithub/zerto-hvm-migration-prep/blob/master/docs/linux-sudoer-setup.md).
5. `report_dir` is set to a stable local path on the MCP server (your
   workstation or wherever vmware-mcp-server runs) so per-VM JSON
   summaries accumulate.
6. For Windows VMs: a virtio-win ISO is available in a vCenter
   Content Library, accessible by `mount_content_library_iso` (e.g.
   item name `virtio-win-0.1.285`).
7. ZertoMCP, Morpheus MCP, and (optionally) Cohesity MCP are
   configured in `claude_desktop_config.json` and reachable.

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

## Stage 1 — Verify rollback insurance

Confirm there is a recent Cohesity backup of the source VM. This is
the rollback path if Stage 2 (prep) does anything unexpected to the
source VM. **Do not skip this.** The prep is well-tested but every
production VM has its own quirks, and offline DISM in WinRE is a
non-trivial operation.

| Step | Tool | Notes |
|---|---|---|
| 1.1 | `cohesity.<list-protection-runs>` (or operator confirms in UI) | Verify a successful backup of the source VM exists within an acceptable RPO (e.g. last 24 hours). |
| 1.2 | If absent: trigger an on-demand backup | Wait for it to complete before proceeding. |

Without rollback insurance, a Stage 2 failure could leave the source
VM in an inconsistent state with no clean restore path. Zerto
checkpoints help but Cohesity gives you a fully independent restore.

---

## Stage 2 — Apply prep to production VM

Inject the VirtIO drivers needed for KVM boot, plus disable conflicting
VMware Tools services. The flow is OS-specific.

### Linux path

| Step | Tool | Notes |
|---|---|---|
| 2L.1 | `vmware-mcp-server.run_in_guest_via_vix` | `script_path` = `prep-linux.sh`. Single tool call. No reboot. Installs `qemu-guest-agent`, ensures virtio modules are in initramfs. |
| 2L.2 | Verify | Tool returns the JSON summary; confirm `passed: true`. |

The Linux path is non-disruptive — no reboot, services keep running,
the VM keeps doing its job until cutover.

### Windows path

The Windows path is a three-phase state machine: **Stage** (no reboot),
**Arm** (one ~50-second WinRE reboot), **Verify** (auto-runs on
post-reboot Windows). Driver injection on Windows requires offline
DISM via WinRE — Microsoft does not expose an online API equivalent.
The whole flow is automated; no human interaction during the reboot.

| Step | Tool | Notes |
|---|---|---|
| 2W.1 | `vmware-mcp-server.list_content_library_isos` | Confirm virtio-win ISO is visible (one-time per session). |
| 2W.2 | `vmware-mcp-server.mount_content_library_iso` | Pass VM name + item name. ISO appears as a CD/DVD in the guest; note the drive letter (typically D:). |
| 2W.3 | `vmware-mcp-server.run_in_guest_via_vix` | `args="-Mode Stage -IsoDrive D:"`. **Non-disruptive.** Copies drivers + scripts to `C:\Zerto-MigrationPrep\`, customizes the WinRE WIM (embeds drivers, wires winpeshl.ini auto-run), registers a one-shot `ZertoMigrationPrep-Verify` startup task. Does NOT reboot. Safe to leave armed across normal reboots. |
| 2W.4 | `vmware-mcp-server.unmount_iso_from_vm` | Drivers are now staged on disk and embedded in the WIM; ISO is no longer needed. |
| 2W.5 | `vmware-mcp-server.run_in_guest_via_vix` | `args="-Mode Arm"`. **This is the one reboot.** Calls `reagentc /boottore`, triggers shutdown. VM goes Windows → WinRE (~10s in) → offline DISM injects viostor + vioscsi + NetKVM → offline registry edits (disable VMware Tools services, set CrashControl AutoReboot=0, iterate every ControlSet) → reboot back to Windows. Total wall-clock ~50 seconds. |
| 2W.6 | Wait + read summary | Wait ~90 seconds. The `ZertoMigrationPrep-Verify` startup task auto-runs when Windows comes back, writing `last-run-summary.json`. Use `run_in_guest_via_vix` with `args="-Mode CheckOnly"` to fetch the summary, OR inspect `report_dir`. Confirm `Passed: true`. |

**Validated end-to-end on:** Server 2019 (build 17763), Server 2025
(build 26100). Same script handles every Server version via build-
number detection — picks the right `2k16` / `2k19` / `2k22` / `2k25`
driver subfolder from the virtio-win ISO automatically. Server 2016
and 2022 should work via the same flow but have not been tested
against an actual KVM/HVM cutover.

**Rollback if Stage 2 fails:** restore from the Cohesity backup
created in Stage 1. Do not proceed to Stage 3 until prod is confirmed
healthy.

---

## Stage 3 — Failover-test on the HVM VPG (KVM bring-up)

The actual moment of truth. We bring up a copy of the prepped VM on
the KVM/HVM target via Zerto FOT. If the prep worked, the VM boots
clean. If it didn't, we see a bluescreen / kernel panic / boot loop
here, where it's recoverable.

| Step | Tool | Notes |
|---|---|---|
| 3.1 | `ZertoMCP.core_vpgs_get_checkpoints` | Latest checkpoint of the VMware → HVM VPG. Pick one taken AFTER Stage 2 completed. |
| 3.2 | `ZertoMCP.core_vpgs_failover_test` | FOT against the HVM VPG. |
| 3.3 | Confirm boot | Out-of-band: KVM console, ping, Morpheus discovery picking up the VM. Look for normal services running, an IP address (NetKVM working), and the Windows login screen — not a recovery prompt or BSOD. |

**If Stage 3 fails:** stop the FOT, do NOT proceed. Roll back to the
Cohesity backup from Stage 1, investigate, iterate on the prep flow,
re-attempt from Stage 2.

---

## Stage 4 — Stop the HVM FOT

Cleanup before the live move. The FOT proved the VM boots; the test
copy is no longer needed.

| Step | Tool | Notes |
|---|---|---|
| 4.1 | `ZertoMCP.core_vpgs_stop_test` | Test failover VM destroyed. The VMware → HVM replication continues. |

After Stage 4: only the production VMware → HVM VPG remains. Verify
with `core_vpgs_list`.

---

## Stage 5 — Live move to HVM

`core_vpgs_move` shuts the source VM down on VMware and brings the
recovery VM up on KVM. The move is **not committed yet** — rollback is
still possible via `core_vpgs_move_rollback` until Stage 7.

| Step | Tool | Notes |
|---|---|---|
| 5.1 | `ZertoMCP.core_vpgs_get_checkpoints` | Latest checkpoint of the HVM VPG. |
| 5.2 | `ZertoMCP.core_vpgs_move` | Source down on VMware, recovery up on KVM. |
| 5.3 | Confirm reachability | Out-of-band: ping, console, Morpheus discovery. Should match Stage 3 result (clean boot, network up). |

**If Stage 5 produces an unhealthy VM:** `core_vpgs_move_rollback`
brings the source VM back up on VMware. Source-side state from before
the move is preserved.

---

## Stage 6 — Morpheus onboarding on KVM

vmware-mcp-server cannot reach this VM anymore — it's no longer in
vCenter. From here, work goes through Morpheus.

> **Tool-loading note:** the Morpheus MCP currently has the agent
> action tools loaded (`install_server_agent`, `make_server_managed`,
> `servers_upgrade`) but the server-discovery tools (`list_servers`,
> `get_server`, `update_server`) may need to be loaded via tool_search
> at session start.

| Step | Tool | Notes |
|---|---|---|
| 6.1 | `morpheus.list_instances` (or `list_servers` if loaded) | Find the moved VM's server ID once Morpheus discovery picks it up. |
| 6.2 | `morpheus.make_server_managed` with `installAgent: true` | Onboard + install agent in one call (brownfield path). |
| 6.3 | `morpheus.list_instances` (or `get_server`) | Verify `agentInstalled: true` and recent heartbeat. |
| 6.4 | Uninstall VMware Tools | Run via Morpheus's command-execution feature. Windows: `Get-Package "VMware Tools" \| Uninstall-Package -Force`. Linux: `apt remove open-vm-tools` / `dnf remove open-vm-tools`. **Order matters: install Morpheus agent FIRST, uninstall VMware Tools SECOND.** Reversing the order leaves no remote management channel between the two steps. |

If Stage 6 fails (agent install fails, host won't register):
`core_vpgs_move_rollback` is still available. Window for rollback
closes at Stage 7.

---

## Stage 7 — Commit the move

Final step. After commit, the source VM is decommissioned and there
is **no rollback** (other than the Cohesity backup from Stage 1, which
restores to a much older state). Only commit once the VM is fully
validated on KVM with the Morpheus agent reporting healthy.

| Step | Tool | Notes |
|---|---|---|
| 7.1 | `ZertoMCP.core_vpgs_move_commit` | Source destroyed; recovery VM is now the source-of-truth. |

---

## Rollback summary

| Failure point | Rollback action | Result |
|---|---|---|
| Stage 0 (eligibility) | N/A | VM is not migratable; resolve blocker, re-run. |
| Stage 1 (no backup) | N/A | Don't proceed without rollback insurance. |
| Stage 2 (prep) | Restore source VM from the Cohesity backup taken in Stage 1. | Source VM rolled back to pre-prep state. |
| Stage 3 (HVM FOT) | `core_vpgs_stop_test` | Test failover destroyed; source VM and replication untouched. |
| Stage 5 (live move) | `core_vpgs_move_rollback` | Source VM brought back up on VMware. |
| Stage 6 (Morpheus onboarding) | `core_vpgs_move_rollback` | Source brought back; Morpheus side discarded. |
| Stage 7 (after commit) | Restore source from Cohesity backup (Stage 1) | Hours-to-days-old state. Use only as last resort. |

---

## Operator prompts

Two prompts. Pick one based on audience.

### Exec demo prompt

Optimized for live audience: short Claude output per stage, named-MCP
shoutouts, one explicit "wow moment" callout. Paste into Claude
Desktop at the start of the session. Replace `<VM>` and `<HVM_VPG>`.

```
You are demonstrating an automated VMware-to-HVM migration to executives.
Migrating VM <VM> via Zerto VPG <HVM_VPG>.

You have four MCP servers: vmware-mcp-server (vCenter), ZertoMCP
(replication/failover), Cohesity (backup), Morpheus (KVM onboarding).

Rules for the demo:
- Per stage: ONE sentence of context, run the tools, ONE sentence of
  result. Plain English. NEVER dump raw tool output, JSON, or file
  paths. NEVER scroll-bomb the audience.
- Always name the MCP you're using ("Zerto is bringing up the test
  failover...", "Cohesity confirms the backup...").
- Pause for "go" before each stage gate.
- At Stage 2W.5 (the WinRE Arm reboot), explicitly call this out
  before running it: "This is the moment nobody else automates
  cleanly. The VM is about to reboot itself, do offline driver
  injection in Windows Recovery, and come back -- about 50 seconds,
  no human at the console."
- Closing: one paragraph recap. What did we orchestrate, how many
  systems, how long.

Begin: introduce the four MCPs by calling one read-only tool from
each (list_vcenters, core_vpgs_list, list_protection_runs,
list_instances). Then proceed to Stage 0.
```

### Operator runbook prompt

Detailed reasoning, full output, no audience. Use this for actual
production cutovers where you want to see exactly what's happening.

```
Migrate VM <VM> from VMware to HVM via VPG <HVM_VPG>. virtio-win ISO
is virtio-win-0.1.285 in vCenter Content Library.

For each stage:
1. State what this stage protects against.
2. Run the tools.
3. Interpret results.
4. Wait for my explicit "go" before next stage.

Pre-conditions confirmed:
- VM already replicates VMware->HVM via VPG <HVM_VPG>.
- Recent Cohesity backup exists.
- Guest credentials in .env.

Begin at Stage 0.
```

---

## Operational notes

- **Stage 3 is non-negotiable.** The KVM-side failover-test is the
  only stage that proves the prepped VM actually boots on the target
  hypervisor. Skipping it means discovering boot problems during the
  live move instead of during a recoverable test.
- **Conservative time gap between Stage 5 and Stage 7.** A 24-hour
  soak window between the live move and the commit is standard
  practice for non-trivial workloads. Same-session commit is only
  appropriate for stateless services where rollback isn't valuable.
- **The Stage 2 Windows reboot is the only outage in the prep phase.**
  ~50 seconds. Schedule it within an existing maintenance window if
  possible; safe to run anytime if the VM doesn't have hard uptime
  requirements.
- **Stage 1 Cohesity backup is what saves you** if something we
  haven't seen happens during Stage 2. Don't skip it. Don't use a
  stale backup; if the most recent one is more than 24 hours old,
  trigger an on-demand one.
