@echo off
REM ============================================================
REM  NInfer - Qwen3.8-27B NVFP4 on a local RTX 5090 (via WSL2)
REM  OpenAI API : http://127.0.0.1:8080/v1
REM  Anthropic  : http://127.0.0.1:8080/v1/messages
REM
REM  Assumes NInfer is built at ~/ninfer inside WSL and the artifact
REM  is at ~/ninfer/models/qwen3_8_27b_nvfp4.ninfer
REM  Set WSL_DISTRO to pick a distro; default is your default one.
REM
REM  DO NOT use --kv-capacity auto on a machine whose desktop shares
REM  the GPU. Measured on an RTX 5090 with ~5 GiB held by the Windows
REM  desktop: auto takes every free byte (free-after-startup 0.00 MiB),
REM  so WDDM spills to system RAM over PCIe and decode collapses:
REM      --kv-capacity auto    ->  16.4 tok/s
REM      --kv-capacity 131072  -> 153.8-170.1 tok/s   (10.4x faster)
REM  Keep an explicit capacity that leaves >= 3-4 GiB of VRAM free.
REM ============================================================
setlocal
if defined WSL_DISTRO (set "D=-d %WSL_DISTRO%") else (set "D=")
wsl.exe %D% -e bash -c "cd ~/ninfer && ./build/apps/ninfer-serve models/qwen3_8_27b_nvfp4.ninfer --host 0.0.0.0 --port 8080 --api-key local-secret --max-context 131072 --kv-capacity 131072 --kv-dtype int8 --max-concurrency 2 --spec mtp --draft-tokens 3 --lm-head-draft 2>&1 | tee ~/serve.log"
