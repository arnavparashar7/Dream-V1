# Start from the RunPod ComfyUI base image
FROM runpod/worker-comfyui:5.1.0-base

# --- Setup custom node installer ---
COPY src/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

# --- Install Custom Nodes (using the new script) ---
WORKDIR /comfyui
# Use the comfy-node-install script to install your custom nodes
# These names must be recognized by 'comfy node install --mode=remote'
# Check registry.comfy.org or the official 'comfy node install' documentation
# If remote mode doesn't work, you might need to revert to git clone for these specific nodes.
RUN comfy-node-install "XLabs-AI/x-flux-comfyui" "kijai/ComfyUI-KJNodes"

# --- Download Models using comfy-cli ---
# Ensure WORKDIR is still /comfyui for relative paths
# The `--filename` is what you use in your ComfyUI workflow.
# Models will be downloaded to /comfyui/<relative-path>/<filename>

# CLIP Text Encoders
RUN comfy model download --url https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors --relative-path models/clip --filename clip_l.safetensors
RUN comfy model download --url https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors --relative-path models/clip --filename t5xxl_fp8_e4m3fn_scaled.safetensors

# UNET/Diffusion Model
RUN comfy model download --url https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors --relative-path models/checkpoints --filename flux1-dev-kontext_fp8_scaled.safetensors
RUN comfy model download --url https://huggingface.co/Kijai/flux-fp8/resolve/main/flux1-dev-fp8.safetensors --relative-path models/checkpoints --filename flux1-dev-fp8.safetensors

# VAE
RUN comfy model download --url https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors --relative-path models/vae --filename ae.safetensors

# Flux ControlNet Model
RUN comfy model download --url https://huggingface.co/XLabs-AI/flux-controlnet-depth-v3/resolve/main/flux-depth-controlnet-v3.safetensors --relative-path models/controlnet --filename flux-depth-controlnet-v3.safetensors

# --- Setup worker application files ---
# Set the working directory for your application code
# This will be /workspace/worker, where your handler and workflows are.
WORKDIR /workspace/worker

# Create the src directory
RUN mkdir -p /workspace/worker/src

# Copy your start.sh and handler.py
ADD src/start.sh /workspace/worker/start.sh
ADD src/handler.py /workspace/worker/src/handler.py
RUN chmod +x /workspace/worker/start.sh

# Copy your workflows
COPY workflows/ /workspace/worker/workflows/

# Expose the ComfyUI port
EXPOSE 8080
