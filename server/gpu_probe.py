"""Tiny independent GPU probe: is the device wedged system-wide, or only the load process?

Runs one trivial matmul on cuda:0 and reports how long it takes. If the GPU is healthy this
returns in milliseconds even while another process is busy on it. If it blocks, the device
itself is wedged.
"""

import datetime
import sys
import time

import torch

print(f"[{datetime.datetime.now():%H:%M:%S}] probe start", flush=True)
if not torch.cuda.is_available():
    print("no GPU visible", flush=True)
    sys.exit(2)

t0 = time.monotonic()
x = torch.randn(512, 512, device="cuda:0", dtype=torch.float16)
alloc = time.monotonic() - t0
print(f"[{datetime.datetime.now():%H:%M:%S}] alloc ok in {alloc:.2f}s", flush=True)

t1 = time.monotonic()
y = (x @ x).sum().item()
mm = time.monotonic() - t1
print(f"[{datetime.datetime.now():%H:%M:%S}] matmul+sync ok in {mm:.2f}s (result {y:.1f})", flush=True)
print("VERDICT: GPU is RESPONSIVE" if mm < 30 else "VERDICT: GPU is SLOW/WEDGED", flush=True)
