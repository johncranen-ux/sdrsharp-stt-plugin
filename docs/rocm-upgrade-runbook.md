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

**Next experiment (single variable):** re-run the identical 520-request replay with SDR# open
and rendering but its transcription disabled, so it adds GPU load without adding requests. If
crashes appear, hypothesis 1 is confirmed and the fix space changes substantially — forcing
SDR#'s rendering onto the idle Raphael iGPU via Windows graphics preferences would be a
trivial, fully reversible test, and cheaper than any ROCm change.

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
