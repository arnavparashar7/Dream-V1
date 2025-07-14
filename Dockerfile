# Start from the RunPod ComfyUI base image
FROM runpod/worker-comfyui:5.1.0-base

# --- Ensure Git is installed (if it's truly missing) ---
# This line might not be strictly necessary if runpod/worker-comfyui:5.1.0-base
# already includes git in a discoverable path, but it's a safe explicit step.
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# --- Install Custom Nodes (direct git clone) ---
# Change working directory to /comfyui where custom_nodes typically reside
WORKDIR /comfyui

# XLabs-AI/x-flux-comfyui
RUN git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git custom_nodes/XLabs-AI && \
    if [ -f custom_nodes/XLabs-AI/requirements.txt ]; then \
        pip install -r custom_nodes/XLabs-AI/requirements.txt; \
    fi

# For ColorMatch (from comfyui-kjnodes)
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git custom_nodes/comfyui-kjnodes && \
    if [ -f custom_nodes/comfyui-kjnodes/requirements.txt ]; then \
        pip install -r custom_nodes/comfyui-kjnodes/requirements.txt; \
    fi

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
