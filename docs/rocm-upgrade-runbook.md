# ROCm upgrade runbook — recorded state and revert steps

Working document for the 2026-07-29 effort to fix the recurring AMD GPU driver crashes at
the root rather than continue relying on the proxy watchdog to recover from them.

Every change gets appended to the **Change log** at the bottom with its undo step. If you are
reading this to roll something back, start there and work upwards (newest first).

---

## Why this work is happening

`whisper-server` dies randomly mid-request. The plugin shows
`HTTP 503: {"error": "Remote end closed connection without response"}`, the watchdog restarts
the backend within ~15-25s, and the audio chunk that was in flight is lost permanently (the
plugin does not requeue a failed chunk — confirmed in `AudioProcessor.SendChunk`).

Prior sessions attributed this to a Windows kernel GPU timeout (TDR) caused by the ROCm
gfx1100 bug [ROCm#2689](https://github.com/ROCm/ROCm/issues/2689), and tried to reduce GPU
work to stay under the timeout: beam size, VAD, `--no-fallback`, `--no-flash-attn`, and a GPU
keep-alive. None resolved it.

### Evidence collected 2026-07-29 that contradicts the TDR theory

| Check | Result |
|---|---|
| Event **4101** ("display driver stopped responding and has recovered") | **0 in 7 days** |
| `C:\Windows\LiveKernelReports` GPU hang dumps | **none** |
| Event **10111** (**user-mode** driver crash) | **11 in 3 days**, within ~1s of each failure |
| `TdrLevel` / `TdrDelay` / `TdrDdiDelay` registry values | **unset** (Windows defaults) |

A genuine 2-second kernel TDR produces Event 4101 and a `LiveKernelReports` dump. Neither
exists. What fires is a **user-mode driver crash**, which is a different failure class — and
it is why every workload-level knob missed: they tuned the work running on top of the failing
layer rather than the layer itself.

> Note: Event 10111 names the *Microsoft Remote Display Adapter*, not the Radeon. It is used
> here only as a reliable **time marker** that correlates with the failures, not as evidence
> about which device faulted. The session is a console session; RDP is only listening.

### Working hypothesis

The user-mode ROCm stack inside WSL is **two years stale relative to the Windows kernel driver
it marshals to**, and AMD has since replaced that user-mode path entirely:

| Component | This machine | Date |
|---|---|---|
| Windows driver | Adrenalin **26.7.1** (`32.0.31035.1003`) | 2026-07-24 |
| ROCm in WSL | **6.1.3-122**, legacy `hsa-runtime-rocr4wsl-amdgpu` | June 2024 |
| AMD's current supported WSL pairing | ROCDXG (`librocdxg`) + Adrenalin 26.2.2+ + ROCm 7.2.1 | — |

### Ruled out — do not re-test

- **iGPU interference.** The machine has a Raphael iGPU (`0x164E`) alongside the 7900 XTX, so
  `HSA_OVERRIDE_GFX_VERSION=11.0.0` could in principle have been mis-applied to it. It cannot:
  `rocminfo` in WSL enumerates only Agent 1 (CPU) and Agent 2 (`gfx1100`). The iGPU is not
  exposed to ROCm.
- **TdrDelay tuning.** See the evidence table — the kernel timeout is not firing.
- Everything already listed in the `gpu-driver-hang-and-watchdog` notes: `wsl --shutdown`,
  `--no-fallback`, the GPU keep-alive, VAD/beam-search combinations.

---

## Recorded starting state (2026-07-29, before any change)

### Windows

```
GPU        : AMD Radeon RX 7900 XTX  (PCI\VEN_1002&DEV_744C)
Driver     : 32.0.31035.1003  = Adrenalin 26.7.1, dated 2026-07-24
iGPU       : AMD Radeon(TM) Graphics (0x164E), driver 32.0.21045.1000
Windows SDK: 10.0.26100.0   (present — required to build librocdxg)
TDR keys   : TdrLevel/TdrDelay/TdrDdiDelay/TdrLimitTime/TdrLimitCount all unset
```

### WSL

```
Distro  : Ubuntu-22.04  (NOT the default "Ubuntu" distro)
ROCm    : 6.1.3-122   (/opt/rocm -> /opt/rocm-6.1.3)
WSL shim: hsa-runtime-rocr4wsl-amdgpu  1.13.0-1789577.22.04
Toolchain: cmake 3.22.1, gcc 11.4.0     (librocdxg needs >=3.15 / >=11.4 — both satisfied)
rocminfo: Agent 1 = CPU, Agent 2 = gfx1100 (RX 7900 XTX). No other GPU agent.
Disk    : 34 GB used of 1007 GB in the distro
```

whisper.cpp:

```
Source : ~/whisper.cpp @ 97c56f1d ("ruby : add VAD speech segments API (#3931)")
Patch  : ggml/src/ggml-cuda/mma.cuh modified in the working tree (NOT committed upstream)
Builds : ~/whisper.cpp/build-rocm       (older, unused fallback)
         ~/whisper.cpp/build-rocm-new   (in use as of this date)
Models : models/ggml-large-v3.bin, models/ggml-silero-v6.2.0.bin
```

### The whisper.cpp ROCm compile patch

Now preserved at **`server/patches/whisper-cpp-rocm-bf16-init.patch`** (commit `0d22329`).
Before that commit it existed *only* as an uncommitted working-tree edit — one `git checkout`
from being lost, after which rebuilds would fail with a non-obvious compile error.

It replaces four `nv_bfloat162 x[ne] = {{0.0f, 0.0f}};` initialisers with
`{{(unsigned short)0, (unsigned short)0}}`. Verified to apply cleanly to pristine upstream
source at `97c56f1d`.

To reapply after a source update:

```bash
cd ~/whisper.cpp
git apply /mnt/d/Claudecode/projects/SDRSharp-Plugin/server/patches/whisper-cpp-rocm-bf16-init.patch
```

Try building **without** it first on ROCm 7 — it may be unnecessary or fixed upstream.

### `start-whisper-server.sh` as it stood

Version-controlled at `server/start-whisper-server.sh` (commit `62f533f`). Key lines:

```bash
export HSA_OVERRIDE_GFX_VERSION=11.0.0
export LD_LIBRARY_PATH=~/whisper.cpp/build-rocm-new/src:~/whisper.cpp/build-rocm-new/ggml/src:~/whisper.cpp/build-rocm-new/ggml/src/ggml-hip:/opt/rocm/lib:/opt/rocm/llvm/lib
...
./build-rocm-new/bin/whisper-server -m models/ggml-large-v3.bin --host 0.0.0.0 --port 8080 \
  -l en --no-flash-attn --vad -vm models/ggml-silero-v6.2.0.bin
```

Deploy with:

```bash
wsl -d Ubuntu-22.04 -- bash -lc "cp /mnt/d/Claudecode/projects/SDRSharp-Plugin/server/start-whisper-server.sh ~/start-whisper-server.sh && chmod +x ~/start-whisper-server.sh"
```

(The `sed -i 's/\r$//'` step previously needed here is no longer required — `.gitattributes`
now pins `*.sh` to LF.)

### Backup

```
D:\backups\Ubuntu-22.04-preROCm7-2026-07-29.tar
```

Full `wsl --export` of the distro taken before any ROCm change. This is the only complete
rollback: notes cannot reinstall ROCm 6.1.3 / `rocr4wsl` if AMD has retired those packages.

---

## Measuring whether a change worked

`server/stress.py` replays saved clips through the proxy and reports crashes per 100 requests.

```
py stress.py --captures "D:/SDR/SdrSharp/Plugins/SttPlugin/captures/2026-07-28" \
             --passes 2 --label baseline-rocm-6.1.3
```

- 260 clips x 2 passes = 520 requests. At the historically observed ~1-in-50 rate that is
  ~10 crashes at baseline — enough to detect a large effect (10 -> 0-1), **not** a subtle one.
- Goes through the proxy on :9000 so the watchdog keeps an unattended run alive, and so the
  backend requests match production exactly.
- **Close SDR# during runs.** It shares the GPU; leaving it open changes the load between
  runs and invalidates the comparison.
- Cross-check the reported failure timestamps against Windows Event 10111 (the command is
  printed at the end of each run).

### Results

| Label | ROCm | SDR# | Requests | Crashes | Per 100 | Notes |
|---|---|---|---|---|---|---|
| `baseline-rocm-6.1.3` | 6.1.3 | **closed** | 520 | **0** | 0.00 | 2026-07-29 17:59-18:39, 39.8 min |

### The baseline did not reproduce the bug — this invalidated the measurement plan

520 consecutive requests on the supposedly-broken ROCm 6.1.3 stack produced **zero** failures,
zero Event 10111s, and `whisper-server` ran 42 minutes on a single PID without one watchdog
restart. For contrast, the same machine on the same day with SDR# running logged three crashes
between 10:32 and 11:00.

**The synthetic replay is therefore not a valid before/after metric on its own.** Upgrading
ROCm and re-running it would produce 0 crashes either way and prove nothing — precisely the
trap this harness was built to avoid. Work stopped here rather than proceeding blind.

**What the replay did not have that production does:**

1. **SDR# running.** It renders a continuous spectrum + waterfall on the same GPU, so
   production has a second concurrent user-mode GPU client that the replay lacks. This is now
   the leading hypothesis, and it reframes an earlier dismissal: Event 10111 names the
   *display* adapter, which was written off as incidental. If the fault involves contention
   on the graphics path, that naming is a clue rather than noise.
2. **Bursty pacing.** The replay sends back-to-back requests ~4.6s apart; real VHF has long
   idle gaps. A previous session tried to test this with a GPU keep-alive and concluded it
   was disproven — but that test ran with SDR# open, so it never separated the two factors.

Audio content is *not* a candidate: the replayed clips are the real captured live audio.

### CORRECTION — the GPU *did* hang during that run; the detector was wrong

The user observed three AMD driver-timeout popups during the "clean" run. Investigating that
overturned two earlier conclusions in this document.

**1. "No `LiveKernelReports` GPU hang dumps" was a false negative.**
`C:\Windows\LiveKernelReports` cannot be listed without elevation and returns *empty* rather
than an error when unprivileged. Reading that as "no dumps exist" was wrong. **107 dumps
exist.** The reliable, unprivileged route is the Windows Error Reporting event each dump
raises — see `server/gpuhangs.py`, which now automates this.

**2. The correct signal is `LiveKernelEvent 141`, not Event 4101 or Event 10111.**
Event 4101 genuinely never fires. But `LiveKernelEvent 141` — the GPU-hang live-dump class —
fires constantly. Note the WER event's own timestamp is the *upload* time (they arrive in
bulk); the real hang time is in the dump filename, `WATCHDOG-YYYYMMDD-HHMM.dmp`.

Because this is a GPU **timeout** event class, the earlier dismissal of `TdrDelay` was based
on bad evidence and that option is back in play.

Recorded hang history:

| Date | Hangs |
|---|---|
| 2026-05-31 | 3 |
| 2026-07-27 | 37 |
| 2026-07-28 | 50 |
| 2026-07-29 | 17 |

### The real finding: a GPU hang and a lost transcription are separable

The 520-request run spanned **2 hangs (18:00, 18:08) with 0 failed requests**, and throughput
did not even dip — the 25-request checkpoint bracketing the 18:08 hang took 114s, identical to
every other checkpoint.

So the hardware faults at a fairly steady rate under load, the driver usually recovers
transparently, and a user-visible 503 is only the subset where a hang stalls an inference past
the proxy watchdog's 25s threshold — at which point the watchdog kills the backend and *that*
produces the error. **Request failures measure a subset of hangs; hangs/hour is the metric.**

Two measurement traps to avoid repeating:
- The AMD popup undercounts: while one dialog is open, further hangs raise no new dialog.
- Request-failure counts miss every hang the driver recovers from.

### Suggestive: the hangs may not be WSL-specific

The three 2026-05-31 hangs (23:40, 23:44, 23:45) land inside the window when AMD's AI Bundle
(ComfyUI + Ollama, `torch 2.9.0+rocmsdk20251116`) was installed — 23:44 to 23:56 by file
timestamps. That is **native Windows ROCm, no WSL involved**. If those hangs came from that
workload, the fault is in the GPU/driver under ROCm compute generally, which would mean Phase 3
(native Windows HIP) does not fix it either.

Three events is not proof and what was running is not confirmed — treat as a lead, not a
conclusion. But it is worth checking before investing in the Phase 3 rewrite.

### Baseline (corrected)

| Label | ROCm | SDR# | Duration | Hangs | Hangs/hr | Req failures |
|---|---|---|---|---|---|---|
| `baseline-rocm-6.1.3` | 6.1.3 | closed | 39.8 min | 2 | **3.0** | 0 / 520 |

---

## DECISIVE: the fault is not WSL's. Phases 2 and 3 are both dead ends.

**Test (2026-07-29 19:38):** sustained fp16 GEMM + softmax load on the 7900 XTX using the
**native Windows** ROCm stack — AMD AI Bundle's PyTorch `2.9.0+rocmsdk20251116`, **HIP 7.1**.

Conditions, all verified at the time of the wedge:

| | |
|---|---|
| WSL | **all distros shut down** (`wsl --shutdown`, "no running distributions") |
| whisper-server / proxy | not running (0 processes) |
| SDR# | closed |
| GPU work | native Windows HIP only |

**Result: the GPU wedged hard, ~40-52 minutes in.**

- The load process spun at 100% of one core, holding 820 MB of GPU memory, stuck inside a
  single iteration for 13+ minutes past its deadline (a normal iteration is ~5-10 ms).
- An **independent** process could not run a trivial 512x512 matmul — timed out at 120s. The
  device was wedged system-wide, not just for the loading process.
- `Stop-Process -Force` **could not kill it** — stuck in an uninterruptible kernel GPU wait.
- Killing the process did **not** free the GPU; a fresh probe still timed out.
- **Zero `LiveKernelEvent 141` fired.** This wedge produced no TDR at all — a distinct, more
  severe failure mode than the 107 recorded hangs, which do self-recover.

### What this rules out

- **Phase 2 (upgrade WSL ROCm 6.1.3 -> 7.2.1 + ROCDXG).** WSL was entirely shut down. The
  version-skew hypothesis, however reasonable, cannot explain a hang that happens with WSL
  not running. Upgrading it will not fix this.
- **Phase 3 (rebuild whisper.cpp for native Windows HIP).** That configuration is what just
  wedged, on **HIP 7.1** — six releases newer than the WSL stack. Porting to it would move
  the problem, not remove it.
- **Any whisper.cpp-level tuning.** whisper.cpp was not running.

### What remains

The fault is in the GPU or its Windows kernel driver under sustained ROCm compute. Note the
2026-05-31 hangs predate the current Adrenalin 26.7.1 (dated 2026-07-24), so this is not a
regression introduced by the recent driver either. Consistent with
[ROCm#2689](https://github.com/ROCm/ROCm/issues/2689) being unresolved for years on this exact
GPU.

Honest remaining options, best first:

1. **Power/clock limiting** via Adrenalin tuning (e.g. -10% power limit, or capping max clock).
   Reducing transient power draw is the most commonly reported mitigation for RDNA3 compute
   hangs, it is free, and it is reversible in seconds. Cheapest real test left.
2. **PSU / cabling check.** The 7900 XTX draws very large instantaneous spikes; AMD-adjacent
   guidance calls for a quality 850W+ unit with **independent** cables to each 8-pin connector
   rather than a daisy-chained pigtail. A marginal rail would produce exactly this symptom.
3. **Driver rollback** to an older Adrenalin. Weakened by the 2026-05-31 hangs predating the
   current driver, but not excluded.
4. **Accept and mitigate.** The existing proxy watchdog already handles the *recoverable*
   hangs well. Note it cannot help with a hard wedge like this one, which needs a reboot.

**Recovery from a hard wedge: full power-off, not a reboot.** Killing the process did not free
the GPU, and a **warm reboot did not clear it either** — only a full power cycle brought the
card back (confirmed 2026-07-29). After the power cycle, `rocminfo` in WSL enumerated `gfx1100`
normally and whisper-server served a real inference in 0.7s.

That detail is diagnostic, not just operational: a warm reboot re-initialises the driver but
does not necessarily cut power to the card or force a full PCIe/device reset. A fault that
survives driver re-init and only clears on power removal sits in device or firmware state the
driver cannot reset — which strengthens options 1 and 2 above (power/clock limiting, PSU and
cabling) relative to option 3 (driver rollback).

---

## Change-history trace: nothing introduced the crashes

Done 2026-07-29 evening, after the wedge. Every prior session tested hypotheses *forward*
(change a setting, see if it still crashes). This instead traced *backward* from the hang
record to find what changed before the onset. Result: there is no onset to explain.

Full hang record from `server/gpuhangs.py --since 2026-05-01`, interleaved with what the
machine was doing:

| When | What happened | Hangs |
|---|---|---|
| 5/13 19:57 | **Windows Update** installs AMD Display `31.0.24002.92` | — |
| 5/30 23:53 | Initial plugin release committed (`a00aa47`) | — |
| 5/31 23:24–23:27 | AMD Software installer runs (`C:\AMD\AMDSoftwareInstaller` ctime) | — |
| 5/31 23:40 / 23:44 / 23:45 | first real GPU inference use | **3** |
| 6/1 – 7/26 | **no commits — GPU idle** | **0** |
| 7/27 13:13 → 18:51 | heavy live use resumes | **37** |
| *7/27 18:24* | *`ggml-large-v3.bin` downloaded — after 36 of that day's 37 hangs* | |
| *7/27 18:52* | *pipeline rework committed (`1dc6769`) — after all but one* | |
| 7/28 | full day of use | **50** |
| 7/29 10:32–10:59 | morning use | **5** |
| *7/29 11:01* | *Adrenalin 26.7.1 installed by **AMD's own installer**, not Windows Update* | |
| 7/29 16:29–21:07 | afternoon/evening use | **14** |

**The June–July silence is project dormancy, not GPU health.** No commits between 5/30 and
7/27, and the one other GPU-capable workload on the box never touched the GPU — Ollama's
`server.log` reports `inference compute id=cpu library=cpu`. So the fault has been present on
every single day the GPU ran ROCm compute, starting the day after the initial release.

### Retired by this trace — do not re-test

- **The large-v3 model switch.** 36 of 37 hangs on 7/27 happened *before* `ggml-large-v3.bin`
  finished downloading at 18:24. The machine was still on turbo.
- **The 7/27 pipeline rework** (`1dc6769`, prompt fix / resampling / VAD overhaul). Committed
  18:52, after nearly all that day's hangs.
- **Driver rollback.** 7/27–7/28 hangs ran on the 5/13 Windows Update driver; 7/29 evening
  hangs ran on Adrenalin 26.7.1. Two different drivers, 109 hangs, unchanged rate. Also note
  the 11:01 install today *is* the "update the driver" experiment, run accidentally — the
  afternoon rate did not move.

### Also rejected: switching to large-v3-turbo to cut GPU-busy time

Tempting reasoning — turbo has 4 decoder layers vs large-v3's 32, so it should slash GPU
exposure per request. **The repo's own benchmark refutes it** (`README.md`, Model row): turbo
is only ~25% cheaper in decode time (mean 2.66s vs 3.55s), because Whisper's encoder always
processes a fixed 30-second window and turbo trims only the decoder. Best case 3.0 → ~2.25
hangs/hr, at a cost of 1.9 points of WER (40.8% vs 38.9%). Bad trade; not worth a run.

Note the same fixed-encoder effect measured on CPU (2026-07-29): CPU-only latency is flat at
~6.0–6.5s per request regardless of clip length (0.88s of audio costs the same as 12.5s), and
q5_0 quantization barely helps (6.0s vs 6.4s) — the path is compute-bound, not
memory-bound. CPU-only remains the one option that cannot hang, at ~10x the GPU's latency.

### What the trace leaves standing

The rate is constant across three independent measurements: 3.0 hangs/hr (controlled
520-request run), 3.59 hangs/hr (2026-07-29 evening, 14 min after a cold power-on), and the
daily counts scale with hours of use. Combined with the wedge that occurred with WSL shut
down, this is a hardware or kernel-driver fault that no software configuration in this repo
influences. Remaining options are power/clock limiting, the PSU/cabling check, and RMA.

---

## 2026-07-29 late evening: the card degraded ~6x after the hard wedge

Two runs, same harness / clips / driver / model (large-v3) / SDR# closed, measured with
`gpuhangs.py` over each run's exact window:

| Label | Time | Adrenalin power limit | Card state | Duration | Hangs | Rate |
|---|---|---|---|---|---|---|
| `baseline-rocm-6.1.3` | 18:00 | 0% (default) | **pre**-wedge | 39.8 min | 2 | **3.0/hr** |
| `powerlimit-minus10` | 21:41 | **−10%** | post-wedge | ~6 min | 4 | **~40/hr** |
| `revert-0pct-large-v3` | 22:12 | 0% (reverted) | post-wedge | 35 min | 11 | **18.5/hr** |

Both runs were stopped early once the signal was unambiguous rather than run to completion —
continuing only bought more driver crashes for the user.

### Finding 1: lowering the power limit makes it worse, not better

−10% ran at roughly double the rate of 0% in the same post-wedge state (~40 vs 18.5/hr).
**Keep Adrenalin power tuning at default.** The reasoning that a lower cap means gentler
operation looks backwards: if the fault is triggered by power-state *transients*, capping
lower makes the card bump the ceiling constantly and transition harder and more often.

That inverts the remaining tuning idea — the lever worth trying is *fewer* transitions
(locked/fixed max clock), not a lower ceiling. Untested, and see Finding 2 before bothering.

### Finding 2: the hard wedge appears to have damaged the card

At the identical default power setting, same everything else, the rate went from 3.0/hr to
18.5/hr in four hours — 11 hangs observed in 35 min where 1.75 were expected. Not a sampling
fluke.

**Thermal soak was considered and is a weak explanation:** the 18:00 baseline was itself taken
after a full day of heavy load (hangs at 16:29 through 17:55, right up to the run), so a hot
card was already the baseline condition.

What distinguishes the two periods is the **19:38 hard wedge**, which was categorically
different from the 120 recoverable hangs: no TDR raised, process unkillable, GPU wedged
system-wide for all processes, survived a warm reboot, cleared only by removing power.

**How to apply:** treat this card as failing, not mistuned. Stop spending time on Adrenalin
tuning experiments. Confirmation test, if wanted, is cheap: same 20-min run from a cold boot
in the morning — still ~18/hr confirms degradation and kills the thermal alternative.

### Note: `OD8Settings` is not a usable indicator of custom tuning

Recorded because it was misread once during this session. `OD8Settings = 64` on the display
class key was taken as confirmation that a custom power limit was active. It still read 64
*after* the tuning was reset to default, and its pre-change value was never captured — so it
carries no information either way. What did track the change: `%LOCALAPPDATA%\AMD\CN\gmdb.blb`
mtime, and the presence/absence of `gmrevert.blb` (Adrenalin's tuning-revert snapshot, which
it deletes on reset). Neither exposes the actual percentage — there is no unprivileged way
found to read or set the power limit programmatically; AMD ships no supported CLI for it.

`RGStats.db` in the same folder is a game-session log, not telemetry — no temperature or
clock history available there. `rocm-smi` remains non-functional under WSL2
(`amdgpu not found in modules`), so no GPU temperature readout is available from this side at
all; only Adrenalin's own Performance tab shows it.

---

## Revert steps, cheapest first

1. **Switch the binary back.** Edit `server/start-whisper-server.sh` to point at
   `build-rocm-new` again (the old lines are kept commented in place) and redeploy. Only
   works while ROCm 6.1.3 is still installed.
2. **Reinstall ROCm 6.1.3**, if AMD still publishes it — see the recorded package versions above.
3. **Full distro restore** (always works):
   ```
   wsl --terminate Ubuntu-22.04
   wsl --unregister Ubuntu-22.04
   wsl --import Ubuntu-22.04 <install-dir> D:\backups\Ubuntu-22.04-preROCm7-2026-07-29.tar
   ```
   Note `wsl --unregister` destroys the current distro — be sure the tarball is intact first.
4. **Repo state:** `git revert` the relevant commit, or `git checkout <sha> -- <path>`.

---

## Change log

Newest last. Each entry records what changed and how to undo it.

### 2026-07-29 — Phase 0: preserve and make revertible

| # | Change | Undo |
|---|---|---|
| 1 | Commit `0d22329`: rescued the mma.cuh ROCm patch into `server/patches/`, added `.gitattributes` pinning `*.patch`/`*.sh` to LF | `git revert 0d22329` (but keep the patch file — losing it again is the risk this fixed) |
| 2 | Commit `62f533f`: committed the previously-uncommitted watchdog zombie-listener fix and `server/start-whisper-server.sh` | `git revert 62f533f` |
| 3 | Commit `a2d8916`: added `beam1_prompt` config to `bench.py` | `git revert a2d8916` |
| 4 | Stopped `whisper-proxy.py` and terminated the Ubuntu-22.04 distro to take a consistent backup | Restart with `server/start-all.bat` |
| 5 | Created `D:\backups\Ubuntu-22.04-preROCm7-2026-07-29.tar` | Delete the file to reclaim ~34 GB, once the upgrade is confirmed good |
| 6 | Added `server/stress.py` (crash-rate harness) | `git rm server/stress.py` — measurement only, affects nothing at runtime |
