"""Sustained fp16 GEMM load on the RX 7900 XTX via native Windows ROCm/HIP.

Diagnostic probe: does this GPU also hang (LiveKernelEvent 141) under ROCm compute when WSL
is completely shut down? If it does, the WSL paravirtualization layer is not the cause and
both the WSL ROCm upgrade and the native-HIP fallback are dead ends.

Workload shape deliberately mirrors whisper inference: repeated large fp16 matmuls plus a
softmax/attention-ish step, run back-to-back with no idle gaps, for a duration matching the
39.8-minute WSL baseline.
"""

import argparse
import datetime
import sys
import time

import torch


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=40.0)
    ap.add_argument("--dim", type=int, default=4096)
    args = ap.parse_args()

    if not torch.cuda.is_available():
        print("ERROR: no GPU visible to torch", file=sys.stderr)
        return 2

    dev = torch.device("cuda:0")
    print(f"device      : {torch.cuda.get_device_name(0)}")
    print(f"torch / hip : {torch.__version__} / {torch.version.hip}")
    print(f"duration    : {args.minutes} min, dim={args.dim}\n")

    a = torch.randn(args.dim, args.dim, device=dev, dtype=torch.float16)
    b = torch.randn(args.dim, args.dim, device=dev, dtype=torch.float16)

    start = time.monotonic()
    deadline = start + args.minutes * 60
    iters = 0
    errors = 0
    last_report = start

    while time.monotonic() < deadline:
        try:
            # GEMM chain + attention-like softmax, the dominant kernels in whisper decode.
            c = a @ b
            c = torch.softmax(c.float(), dim=-1).half()
            d = c @ a
            _ = d.sum().item()          # forces a sync each iteration, so a hang shows up here
            iters += 1
        except Exception as exc:        # noqa: BLE001 - a HIP fault is itself the signal
            errors += 1
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] iteration {iters}: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2.0)

        now = time.monotonic()
        if now - last_report >= 120:
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            mins = (now - start) / 60
            print(f"[{stamp}] {mins:5.1f} min  iters={iters}  errors={errors}", flush=True)
            last_report = now

    total = (time.monotonic() - start) / 60
    print(f"\nfinished: {total:.1f} min, {iters} iterations, {errors} errors")
    print(f"started {datetime.datetime.fromtimestamp(time.time() - total * 60):%Y-%m-%d %H:%M:%S}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
