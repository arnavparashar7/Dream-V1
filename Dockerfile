# Stage 1: Base image with common system dependencies and global Python setup
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
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-venv \
    git \
    wget \
    curl \
    unzip \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
    && update-alternatives --install /usr/bin/pip3 pip3 /usr/bin/pip3 1 \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

# --- Installer Stage: Installs ComfyUI, uv, virtual environment, and all Python dependencies ---
FROM base AS installer

# Install uv (latest) using official installer and create isolated venv
# uv is installed globally first, then used to create and populate the venv
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

# Use the virtual environment for all subsequent commands in this stage
ENV PATH="/opt/venv/bin:${PATH}"

# Install comfy-cli + dependencies needed by it to install ComfyUI into the venv
RUN uv pip install comfy-cli pip setuptools wheel

# Change working directory for ComfyUI installation
WORKDIR /comfyui

# Install ComfyUI into this workspace using comfy-cli
# The `--workspace /comfyui` makes comfy-cli install ComfyUI here.
# The `uv venv /opt/venv` and `ENV PATH` ensure comfy-cli operates within the venv.
RUN /usr/bin/yes | comfy --workspace /comfyui install --version 0.3.43 --cuda-version 12.6 --nvidia

# Support for the network volume - ensure this path is correct relative to the build context
# Assuming src/extra_model_paths.yaml is in the build context root's src/ directory
ADD src/extra_model_paths.yaml ./

# Install Python runtime dependencies for the handler into the venv
# These are the user's custom requirements that handler.py explicitly uses
RUN uv pip install runpod requests websocket-client

# --- Custom Nodes Installation ---
# Install custom nodes and their requirements directly into the ComfyUI installation.
# These will use the /opt/venv Python due to ENV PATH.

# ComfyUI-Flux-Nodes contains core Flux nodes
RUN git clone https://github.com/Comfy-Org/ComfyUI-Flux-Nodes.git custom_nodes/ComfyUI-Flux-Nodes && \
    if [ -f custom_nodes/ComfyUI-Flux-Nodes/requirements.txt ]; then \
        uv pip install -r custom_nodes/ComfyUI-Flux-Nodes/requirements.txt; \
    fi

# XLabs-AI/x-flux-comfyui also useful for some Xlabs specific Flux nodes
RUN git clone https://github.com/XLabs-AI/x-flux-comfyui.git custom_nodes/XLabs-AI && \
    if [ -f custom_nodes/XLabs-AI/requirements.txt ]; then \
        uv pip install -r custom_nodes/XLabs-AI/requirements.txt; \
    fi

# For ColorMatch (from comfyui-kjnodes)
RUN git clone https://github.com/kijai/ComfyUI-KJNodes.git custom_nodes/comfyui-kjnodes && \
    if [ -f custom_nodes/comfyui-kjnodes/requirements.txt ]; then \
        uv pip install -r custom_nodes/comfyui-kjnodes/requirements.txt; \
    fi

# --- Downloader Stage: Downloads models to a specific location ---
# Start from installer to get uv and comfy-cli environment for downloading
FROM installer AS downloader

ARG HUGGINGFACE_ACCESS_TOKEN
ARG MODEL_TYPE=flux1-dev-fp8

# Change working directory to ComfyUI for model downloads
WORKDIR /comfyui

# Create necessary directories upfront
RUN mkdir -p models/checkpoints models/vae models/unet models/clip models/controlnet models/text_encoders

# Download models using wget. Ensure correct paths for downloaded files.
# CLIP Text Encoders
RUN wget -O models/text_encoders/clip_l.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors
RUN wget -O models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors

# UNET/Diffusion Model
RUN wget -O models/checkpoints/flux1-dev-kontext_fp8_scaled.safetensors https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors

# VAE
RUN wget -O models/vae/ae.safetensors https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors

# Flux ControlNet Model
RUN wget -O models/controlnet/flux-depth-controlnet-v3.safetensors https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/controlnet/flux-depth-controlnet-v3.safetensors

# --- Final Image Stage: Combines all necessary components for runtime ---
FROM base AS final

# Set the virtual environment path for runtime
ENV PATH="/opt/venv/bin:${PATH}"

# Copy the virtual environment from the installer stage
# This copies all installed Python packages (ComfyUI, runpod, custom node deps)
COPY --from=installer /opt/venv /opt/venv

# Copy the ComfyUI installation itself from the installer stage
COPY --from=installer /comfyui /comfyui

# Copy models from the downloader stage
COPY --from=downloader /comfyui/models /comfyui/models

# Set the working directory for the worker's application code
WORKDIR /workspace/worker

# Create the src directory inside the worker directory
RUN mkdir -p /workspace/worker/src

# Add application code and scripts
# Assuming handler.py is in src/, and start.sh is in src/
# COPY src/start.sh /workspace/worker/start.sh
# COPY src/handler.py /workspace/worker/src/handler.py
# COPY src/test_input.json /workspace/worker/src/test_input.json # if needed
# Use ADD for convenience if start.sh/test_input.json are directly in src/
ADD src/start.sh /workspace/worker/start.sh
ADD src/handler.py /workspace/worker/src/handler.py

RUN chmod +x /workspace/worker/start.sh # Make start.sh executable

# Copy workflows
COPY workflows/ /workspace/worker/workflows/

# Copy helper scripts
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-node-install /usr/local/bin/comfy-manager-set-mode

# Set environment variables for ComfyUI access (if needed by handler.py)
ENV COMFYUI_HOST=127.0.0.1
ENV COMFYUI_PORT=8080

# Expose the ComfyUI port (if you want to access it directly, typically not for worker)
EXPOSE 8080

# Set the default command to run when starting the container
# This will execute the start.sh script, which then should launch ComfyUI and handler.py
CMD ["/workspace/worker/start.sh"]
