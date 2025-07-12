# Start from the RunPod ComfyUI base image
FROM runpod/worker-comfyui:5.1.0-base

# Set environment variables for ComfyUI access (if needed by handler.py)
ENV COMFYUI_HOST=127.0.0.1
ENV COMFYUI_PORT=8080

# --- Install Custom Nodes using comfy-cli ---
# The comfy-node-install command directly installs nodes from their GitHub repos.
# These will be installed into /comfyui/custom_nodes/
RUN comfy-node-install XLabs-AI/x-flux-comfyui kijai/ComfyUI-KJNodes

# --- Download Models using comfy-cli ---
# The `--filename` is what you use in your ComfyUI workflow.
# Models will be downloaded to /comfyui/<relative-path>/<filename>

# CLIP Text Encoders
RUN comfy model download --url https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors --relative-path models/text_encoders --filename clip_l.safetensors
RUN comfy model download --url https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors --relative-path models/text_encoders --filename t5xxl_fp8_e4m3fn_scaled.safetensors

# UNET/Diffusion Model
RUN comfy model download --url https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors --relative-path models/checkpoints --filename flux1-dev-kontext_fp8_scaled.safetensors

# VAE
RUN comfy model download --url https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors --relative-path models/vae --filename ae.safetensors

# Flux ControlNet Model
RUN comfy model download --url https://huggingface.co/XLabs-AI/flux-controlnet-depth-v3/resolve/main/flux-depth-controlnet-v3.safetensors --relative-path models/controlnet --filename flux-depth-controlnet-v3.safetensors

# --- Install additional Python dependencies for your handler.py ---
# The base image already has runpod, PyTorch, xformers, etc.
# We'll install `websocket-client`, `requests`, and `opencv-python` specifically,
# as they were in your `requirements.txt` and might not be in the base image.
# We'll also re-install runpod to ensure you have the correct version.
COPY requirements.txt /tmp/user_requirements.txt
RUN pip install -r /tmp/user_requirements.txt

# --- Setup worker application files ---
# Set the working directory for your application code
WORKDIR /workspace/worker

# Create the src directory
RUN mkdir -p /workspace/worker/src

# Copy your start.sh and handler.py
ADD src/start.sh /workspace/worker/start.sh
ADD src/handler.py /workspace/worker/src/handler.py
RUN chmod +x /workspace/worker/start.sh

# Copy your workflows
COPY workflows/ /workspace/worker/workflows/

# Copy helper scripts (ensure these exist in your scripts/ folder)
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-node-install /usr/local/bin/comfy-manager-set-mode

# Copy local static input files (Optional - only if you have an 'input' folder in your repo root)
# If you have an 'input' folder next to your Dockerfile with static files, uncomment this
# COPY input/ /comfyui/input/

# Expose the ComfyUI port
EXPOSE 8080

# Set the default command to run when starting the container
CMD ["/workspace/worker/start.sh"]
