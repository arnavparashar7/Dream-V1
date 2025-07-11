#!/usr/bin/env bash

# Use libtcmalloc for better memory management (if available)
TCMALLOC="$(ldconfig -p | grep -Po "libtcmalloc.so.\d" | head -n 1)"
export LD_PRELOAD="${TCMALLOC}"

# Ensure ComfyUI-Manager runs in offline network mode inside the container
# This script needs to be present and executable, as per Dockerfile
comfy-manager-set-mode offline || echo "worker-comfyui - Could not set ComfyUI-Manager network_mode" >&2

echo "worker-comfyui: Starting ComfyUI"

: "${COMFY_LOG_LEVEL:=DEBUG}"

# Start ComfyUI using the Python from the virtual environment
# Ensure it listens on 0.0.0.0 so RunPod can access it
/opt/venv/bin/python -u /comfyui/main.py --listen 0.0.0.0 --port 8080 --disable-auto-launch --disable-metadata --verbose "${COMFY_LOG_LEVEL}" --log-stdout &

# Wait for ComfyUI to actually start
echo "Waiting for ComfyUI to start..."
sleep 10 # Give ComfyUI some time to initialize

echo "worker-comfyui: Starting RunPod Handler"

# Navigate to the worker's source directory
cd /workspace/worker/src || { echo "Failed to change directory to /workspace/worker/src"; exit 1; }

# Start the RunPod handler using the Python from the virtual environment
# Always use /opt/venv/bin/python for the handler to ensure runpod is found
if [ "$SERVE_API_LOCALLY" == "true" ]; then
    /opt/venv/bin/python -u handler.py --rp_serve_api --rp_api_host=0.0.0.0
else
    /opt/venv/bin/python -u handler.py # Ensure this also uses the venv python
fi

# Keep the script running to keep the container alive for RunPod.
# This waits for any background process to finish.
wait -n

# Exit with the status of the process that exited first.
exit $?
