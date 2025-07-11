# Stage 0: Base image with common system dependencies and global Python setup
# Using a specific NVIDIA CUDA image with Ubuntu 22.04 for stability
FROM nvcr.io/nvidia/cuda:12.1.1-devel-ubuntu22.04 AS base

# Prevent prompts from packages asking for user input during installation
ENV DEBIAN_FRONTEND=noninteractive
# Prefer binary wheels over source distributions for faster pip installations
ENV PIP_PREFER_BINARY=1
# Ensures output from python is printed immediately to the terminal without buffering
ENV PYTHONUNBUFFERED=1
# Speed up some cmake builds
ENV CMAKE_BUILD_PARALLEL_LEVEL=8

# Install Python 3.10, its venv module, and other core system tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3.10-distutils \
    python3.10-venv \
    python3-pip \
    wget \
    curl \
    unzip \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    # Clean up apt cache to reduce image size
    && rm -rf /var/lib/apt/lists/*

# Link python3 to python3.10 and pip3 to pip for consistency
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

# Install git separately to ensure it's available for git clone commands
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Verification step to confirm git is installed and in PATH
RUN git --version

# Stage 1: Installer - Set up ComfyUI, uv, virtual environment, and install all Python dependencies
FROM base AS installer

# Install uv (latest) using official installer and create isolated venv
# uv is installed globally first, then used to create and populate the venv
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

# Use the virtual environment for all subsequent commands in this stage
ENV PATH="/opt/venv/bin:${PATH}"

# Clone ComfyUI directly into /comfyui
WORKDIR /comfyui
RUN git clone https://github.com/comfyanonymous/ComfyUI.git .

# Install ComfyUI's core requirements (from its own requirements.txt)
RUN uv pip install --system -r requirements.txt

# Copy the user's custom requirements.txt from the build context (repo root)
# to a known, distinct location inside the image (e.g., /tmp/user_requirements.txt)
# This ensures YOUR requirements.txt (with 'runpod', 'requests', 'websocket-client') is available
COPY requirements.txt /tmp/user_requirements.txt

# Install all remaining Python dependencies (from user's requirements, xformers, etc.)
# Use PyTorch CUDA 12.1 wheel URL and general PyPI as extra index
RUN uv pip install --system \
    -r /tmp/user_requirements.txt \
    xformers==0.0.22.post7 \
    --index-url https://download.pytorch.org/whl/cu121 \
    --extra-index-url https://pypi.org/simple/

RUN /opt/venv/bin/pip install --upgrade 'runpod>=1.7.12'

# --- Custom Nodes Installation ---
# Install custom nodes and their requirements directly into the ComfyUI installation.
# These will use the /opt/venv Python due to ENV PATH.

# XLabs-AI/x-flux-comfyui contains core Flux nodes
RUN git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git custom_nodes/XLabs-AI && \
    if [ -f custom_nodes/XLabs-AI/requirements.txt ]; then \
        uv pip install -r custom_nodes/XLabs-AI/requirements.txt; \
    fi

# For ColorMatch (from comfyui-kjnodes)
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git custom_nodes/comfyui-kjnodes && \
    if [ -f custom_nodes/comfyui-kjnodes/requirements.txt ]; then \
        uv pip install -r custom_nodes/comfyui-kjnodes/requirements.txt; \
    fi

# Stage 2: Downloader - Downloads models to a specific location
# Start from installer to get uv and comfy-cli environment for downloading
FROM installer AS downloader

ARG HUGGINGFACE_ACCESS_TOKEN
ARG MODEL_TYPE=flux1-dev-fp8

# Change working directory to ComfyUI for model downloads
WORKDIR /comfyui

# Create necessary directories upfront for models
RUN mkdir -p models/checkpoints models/vae models/unet models/clip models/controlnet models/text_encoders

# Download models using wget. All paths are relative to WORKDIR /comfyui.
# CLIP Text Encoders
RUN wget -O models/text_encoders/clip_l.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors
RUN wget -O models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors

# UNET/Diffusion Model
RUN wget -O models/checkpoints/flux1-dev-kontext_fp8_scaled.safetensors https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors

# VAE
RUN wget -O models/vae/ae.safetensors https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors

# Flux ControlNet Model (corrected URL)
RUN wget -O models/controlnet/flux-depth-controlnet-v3.safetensors https://huggingface.co/XLabs-AI/flux-controlnet-depth-v3/resolve/main/flux-depth-controlnet-v3.safetensors

# Stage 3: Final image - Combines all necessary components for runtime
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

# Add application code and scripts from your repository's src/ folder
# Assuming handler.py and start.sh are inside your local src/ directory
ADD src/start.sh /workspace/worker/start.sh
ADD src/handler.py /workspace/worker/src/handler.py

# Make start.sh executable
RUN chmod +x /workspace/worker/start.sh

# Copy workflows from your repository's workflows/ folder
COPY workflows/ /workspace/worker/workflows/

# Copy helper scripts from your repository's scripts/ folder
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
