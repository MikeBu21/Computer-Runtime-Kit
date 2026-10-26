# Diagnosis 2026-09-16

## Root cause
Agent edits 2026-09-15/16 (RAM cache, hybrid git, import conflict) — not SD wear.

## Storage
- Root: /dev/mmcblk0p2 (SC32G SD, 05/2023). No life_time/pre_eol attrs. No mmc I/O error storm.
- Boot: /dev/sda1 (USB VFAT). Kernel reported dirty unmount at boot.
- Snapshot branch: backup/2026-09-16-diagnosis-snapshot (416115d)
- Tag: snapshot-2026-09-16-diagnosis

## Actions taken
See plan todos: freeze agents, fsck boot, git reconcile, runtime fixes.

## Storage check results (2026-09-16)

- SD: SC32G (05/2023); no `life_time`/`pre_eol_info` sysfs attrs (common for consumer SD).
- No mmc I/O error storm in dmesg.
- Boot VFAT `/dev/sda1`: fsck.vfat run after umount (see shell log); remounted.
- Root ext4: offline full fsck deferred via `/forcefsck` if writable (next reboot).
- Recommendation: no SD replacement required now; prefer clean shutdowns.

### fsck privilege note
Interactive `sudo` password required as user `piminer`; non-interactive repair of `/dev/sda1` could not complete in this session.
**Operator command for next maintenance window:**
```bash
sudo umount /boot/firmware
sudo fsck.vfat -a /dev/sda1
sudo mount /boot/firmware
sudo touch /forcefsck   # optional: root fsck on next reboot
sudo reboot
```
Wear: SD SC32G (05/2023), no life_time attrs; no I/O error storm — **no SD swap recommended now**.

## Git reconcile decision (2026-09-16)

**Chosen approach:** Keep Sep live disk state (`backup/2026-09-16-diagnosis-snapshot` / `416115d`) and merge onto `feature/mac-ip-recovery`. Do **not** roll back to Aug 30 HEAD.

### Deletion classification vs `84bacd4`
| Count | Category | Action |
|------:|----------|--------|
| 96 | `miner-export/` legacy | Keep deleted |
| 29 | `myminingfarm*` legacy | Keep deleted |
| 11 | `miner_web*` legacy | Keep deleted |
| 29 | root legacy utils (`scraper.py`, `listener.py`, login scripts, …) | Keep deleted — no live imports |
| 3 | obsolete energy_monitor `*_value.txt` | Keep deleted |

### Critical runtime present
`app.py`, `miner_control.py` (backend), `ram_cache.py`, `control_algorithm_daemon.py`, `start_all.sh` — OK.
`scraper.py` — missing, unused by live stack.

## Runtime fixes applied (2026-09-16)

1. Removed shadowing `energy_monitor/miner_control.py` (Playwright login helper duplicated `dashboard_login_miners.py`); it broke `execute_miner_pause_resume` imports.
2. `control_algorithm/service.py`: append `energy_monitor` to `sys.path` (not insert at 0); keep backend first.
3. `device_registry.py`: clear shadowed `miner_control` module before import.
4. Fixed broken `/home/piMiner/...` symlink: `energy_monitor/settings` → `backend/shared_settings`.
5. Killed duplicate orphan `maintenance_watchdog` (PPID 1); systemd user unit instance remains.
6. Stopped Cursor agent auto-loop `AGENT_LOOP_TICK_pihealth`.
7. Smoke: `execute_miner_pause_resume` imports from backend module; RAM hydrate populates `/dev/shm/minersteuerung`; Flask `/login` → HTTP 200.

### Storage follow-up (needs sudo password on device)
```bash
sudo umount /boot/firmware && sudo fsck.vfat -a /dev/sda1 && sudo mount /boot/firmware
sudo touch /forcefsck && sudo reboot   # optional root fsck
```

## Storage check results (completed 2026-09-16 15:30)

- `fsck.vfat -a /dev/sda1` after umount: **dirty bit cleared**; minor boot-sector vs backup difference left (harmless, not auto-fixed).
- Boot remounted OK at `/boot/firmware`.
- Root ext4 (`mmcblk0p2`): **Filesystem state: clean**; free ~3.7M blocks; last checked 2026-04-21.
- `/forcefsck` created → full root fsck on **next reboot**.
- SD wear: SC32G (05/2023), no `life_time`/`pre_eol_info` — **no SD swap recommended**.

## Power-loss hydrate + Last-Ist fix (2026-09-16)

- After hard power cut: `/dev/shm` is empty — **no manual Flask command**.
- Autostart: `hydrate_ram_after_powerloss.sh` via `miner-ram-hydrate.service` + `ExecStartPre`.
- Last-Ist bug: `betrieb` stayed 0 because missing hydro made `verbrauch_gesamt` n/a; fixed hydro=0 fallback + `line_verbrauch` integration.

