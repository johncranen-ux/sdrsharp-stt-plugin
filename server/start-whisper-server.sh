#!/bin/bash
export HSA_OVERRIDE_GFX_VERSION=11.0.0
export LD_LIBRARY_PATH=~/whisper.cpp/build-rocm-new/src:~/whisper.cpp/build-rocm-new/ggml/src:~/whisper.cpp/build-rocm-new/ggml/src/ggml-hip:/opt/rocm/lib:/opt/rocm/llvm/lib

# whisper-server (cpp-httplib) sets SO_REUSEPORT, so a stuck/zombie instance
# from a previous run does NOT block a new one from binding to :8080 -- they
# silently coexist and the kernel load-balances requests across both, routing
# some fraction to the dead one forever. Kill every PID already on :8080
# before starting a new one, and wait for the port to actually clear.
existing_pids=$(ss -ltnp 2>/dev/null | grep ':8080' | grep -oP 'pid=\K[0-9]+' | sort -u)
if [ -n "$existing_pids" ]; then
  echo "start-whisper-server.sh: killing existing whisper-server pid(s): $existing_pids"
  kill -9 $existing_pids
  for _ in $(seq 1 20); do
    still=$(ss -ltnp 2>/dev/null | grep ':8080')
    [ -z "$still" ] && break
    sleep 0.5
  done
fi

cd ~/whisper.cpp
./build-rocm-new/bin/whisper-server \
  -m models/ggml-large-v3.bin \
  --host 0.0.0.0 \
  --port 8080 \
  -l en \
  --no-flash-attn \
  --vad \
  -vm models/ggml-silero-v6.2.0.bin
