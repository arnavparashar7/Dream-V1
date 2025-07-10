# Stage 1: Base image with common dependencies
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS base

# Prevents prompts from packages asking for user input during installation
ENV DEBIAN_FRONTEND=noninteractive
# Prefer binary wheels over source distributions for faster pip installations
ENV PIP_PREFER_BINARY=1
# Ensures output from python is printed immediately to the terminal without buffering
ENV PYTHONUNBUFFERED=1
# Speed up some cmake builds
ENV CMAKE_BUILD_PARALLEL_LEVEL=8

# Install Python, git and other necessary tools
RUN apt-get update && apt-get install -y \
    python3.12 \
    python3.12-venv \
    git \
    wget \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    && ln -sf /usr/bin/python3.12 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

# Clean up to reduce image size
RUN apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Install uv (latest) using official installer and create isolated venv
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

# Use the virtual environment for all subsequent commands
ENV PATH="/opt/venv/bin:${PATH}"

# Install comfy-cli + dependencies needed by it to install ComfyUI
RUN uv pip install comfy-cli pip setuptools wheel

# Install ComfyUI
RUN /usr/bin/yes | comfy --workspace /comfyui install --version 0.3.43 --cuda-version 12.6 --nvidia

# Change working directory to ComfyUI
WORKDIR /comfyui

# Support for the network volume
ADD src/extra_model_paths.yaml ./

# Go back to the root
WORKDIR /

# Install Python runtime dependencies for the handler
RUN uv pip install runpod requests websocket-client

# Add application code and scripts
ADD src/start.sh handler.py test_input.json ./
RUN chmod +x /start.sh

# Add script to install custom nodes
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

# Prevent pip from asking for confirmation during uninstall steps in custom nodes
ENV PIP_NO_INPUT=1

# Copy helper script to switch Manager network mode at container start
COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-manager-set-mode

# Set the default command to run when starting the container
CMD ["/start.sh"]

# Stage 2: Download models
FROM base AS downloader

ARG HUGGINGFACE_ACCESS_TOKEN
# Set default model type if none is provided.
ARG MODEL_TYPE=flux1-dev-fp8

# Change working directory to ComfyUI
WORKDIR /comfyui

# Create necessary directories upfront
RUN mkdir -p models/checkpoints models/vae models/unet models/clip models/controlnet models/text_encoders

# Download checkpoints/vae/unet/clip models to include in image based on model type
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

# Add any other models if necessary, ensure they go into the correct ComfyUI model sub-folder.
# Example: RUN wget -O /comfyui/models/loras/my_lora.safetensors https://example.com/my_lora.safetensors

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

# --- System-level installations and setup for final image ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        wget \
        libgl1 \
        libglib2.0-0 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Fix any broken dependencies (sometimes needed after initial installs)
RUN apt-get update && apt-get install -f -y && apt-get clean && rm -rf /var/lib/apt/lists/*

# Ensure `git` is in the PATH (if not already)
ENV PATH="/usr/bin/git:$PATH"

# --- Custom Nodes Installation ---
# Note: Use --depth 1 for shallow clones to save space and time
# This one contains core Flux nodes like DualCLIPLoader, DifferentialDiffusion etc.
RUN git clone --depth 1 https://github.com/Comfy-Org/ComfyUI-Flux-Nodes.git /comfyui/custom_nodes/ComfyUI-Flux-Nodes && \
    pip install -r /comfyui/custom_nodes/ComfyUI-Flux-Nodes/requirements.txt

# This one is also useful for some Xlabs specific Flux nodes
RUN git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git /comfyui/custom_nodes/XLabs-AI

# For ColorMatch (from comfyui-kjnodes)
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git /comfyui/custom_nodes/comfyui-kjnodes && \
    pip install -r /comfyui/custom_nodes/comfyui-kjnodes/requirements.txt

RUN pip install requests
