# Stage 0: Base image definition
FROM nvidia/cuda:12.1.1-devel-ubuntu22.04 AS base

# Prevent Python from writing .pyc files
ENV PYTHONDONTWRITEBYTECODE=1

# Set environment for non-interactive apt-get
ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.10, pip, and other common dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3.10 \
        python3.10-distutils \
        python3-pip \
        wget \
        git \
        libgl1 \
        libglib2.0-0 \
        python3-opencv && \
    # Set python3.10 as the default python3
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Upgrade pip and setuptools for the newly installed python3.10
RUN python3 -m pip install --upgrade pip setuptools

# Stage 1: Install ComfyUI and its core dependencies
FROM base AS installer

# Set Python build options for uv
ENV UV_SYSTEM_PYTHON=1
ENV UV_PYTHON_INSTALL_NATIVE_LIBS=1

# Change to /comfyui directory for installation
WORKDIR /comfyui

# Clone ComfyUI
RUN git clone https://github.com/comfyanonymous/ComfyUI.git .

# Install uv for fast dependency management
RUN python3 -m pip install uv

# Install ComfyUI dependencies including torch, torchvision, torchaudio, xformers, opencv-python
# Use the official PyTorch wheel URL for CUDA 12.1
# Force a specific xformers version known to be compatible with PyTorch 2.1.0 and CUDA 12.1
# Note: uv will intelligently handle dependencies and resolve versions.
RUN uv pip install --system \
    -r requirements.txt \
    xformers==0.0.22.post7 \
    --index-url https://download.pytorch.org/whl/cu121 \
    --extra-index-url https://pypi.org/simple/ # Add PyPI as an extra source for other packages

# Stage 2: Download models
FROM base AS downloader

ARG HUGGINGFACE_ACCESS_TOKEN
# Set default model type if none is provided
ARG MODEL_TYPE=flux1-dev-fp8

# Change working directory to ComfyUI
WORKDIR /comfyui

# Create necessary directories upfront
RUN mkdir -p models/checkpoints models/vae models/unet models/clip models/controlnet models/text_encoders

# Download checkpoints/vae/unet/clip models to include in image based on model type
# All paths here are relative to WORKDIR /comfyui
# --- Model Downloads ---
# CLIP Text Encoders
RUN wget -O /comfyui/models/text_encoders/clip_l.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors
RUN wget -O /comfyui/models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors

# UNET/Diffusion Model
RUN wget -O /comfyui/models/checkpoints/flux1-dev-kontext_fp8_scaled.safetensors https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors

# VAE
RUN wget -O /comfyui/models/vae/ae.safetensors https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors

# Flux ControlNet Model
RUN wget -O /comfyui/models/controlnet/flux-depth-controlnet-v3.safetensors https://huggingface.co/XLabs-AI/flux-controlnet-depth-v3/resolve/main/flux-depth-controlnet-v3.safetensors

# Stage 3: Final image
FROM base AS final

# Set ENV variables for the running container (not during build)
ENV PYTHONUNBUFFERED=1 \
    COMFYUI_PATH=/comfyui \
    COMFYUI_MODELS_PATH=/comfyui/models \
    RUNPOD_DEBUG_PORT=5000 \
    UVICORN_PORT=8080 \
    CUDA_VISIBLE_DEVICES=0 \
    PATH="/usr/bin/python3:$PATH" \
    PYTHONPATH=$PYTHONPATH:/comfyui/custom_nodes/AITemplate/python

WORKDIR /workspace/worker

# Copy source code and models from previous stages
COPY --from=installer /comfyui /comfyui
COPY --from=downloader /comfyui/models /comfyui/models
COPY src/ /workspace/worker/src/
COPY workflows/ /workspace/worker/workflows/
COPY scripts/ /workspace/worker/scripts/
COPY .rp_ignore /workspace/worker/.rp_ignore

# --- Custom Nodes Installation ---
# Note: Use --depth 1 for shallow clones to save space and time
# These custom nodes will be installed directly into the ComfyUI folder from the installer stage.
WORKDIR /comfyui

# This one contains core Flux nodes like DualCLIPLoader, DifferentialDiffusion etc.
RUN git clone --depth 1 https://github.com/Comfy-Org/ComfyUI-Flux-Nodes.git custom_nodes/ComfyUI-Flux-Nodes && \
    pip install -r custom_nodes/ComfyUI-Flux-Nodes/requirements.txt

# This one is also useful for some Xlabs specific Flux nodes
RUN git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git custom_nodes/XLabs-AI

# For ColorMatch (from comfyui-kjnodes)
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git custom_nodes/comfyui-kjnodes && \
    pip install -r custom_nodes/comfyui-kjnodes/requirements.txt

WORKDIR /workspace/worker

# Expose ports for ComfyUI and the RunPod worker
EXPOSE 8080 5000

# Set entrypoint for the worker
ENTRYPOINT ["python3", "-u", "/workspace/worker/src/handler.py"]
CMD ["--rp_args"] # For RunPod Serverless, you can pass arguments here if needed
