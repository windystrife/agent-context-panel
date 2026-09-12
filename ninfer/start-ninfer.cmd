@echo off
REM ============================================================
REM  NInfer - Qwen3.8-27B NVFP4 on local RTX 5090 (via WSL2)
REM  OpenAI API : http://127.0.0.1:8080/v1
REM  Anthropic  : http://127.0.0.1:8080/v1/messages
REM  API key    : local-secret
REM ============================================================
REM
REM  DO NOT use --kv-capacity auto on this machine.
REM  Measured 2026-09-12: auto grabs all free VRAM (free-after-startup
REM  = 0.00 MiB), the Windows desktop already holds ~5 GiB, so WDDM
REM  spills to system RAM over PCIe and decode collapses:
REM      --kv-capacity auto    ->  16.4 tok/s
REM      --kv-capacity 131072  -> 153.8-170.1 tok/s   (10.4x faster)
REM  Keep an explicit capacity that leaves >= 3-4 GiB of VRAM free.
REM
wsl.exe -d Ubuntu-24.04 -e bash -c "cd /home/tungnt/ninfer && ./build/apps/ninfer-serve models/qwen3_8_27b_nvfp4.ninfer --host 0.0.0.0 --port 8080 --api-key local-secret --max-context 131072 --kv-capacity 131072 --kv-dtype int8 --max-concurrency 2 --spec mtp --draft-tokens 3 --lm-head-draft 2>&1 | tee /home/tungnt/serve.log"
