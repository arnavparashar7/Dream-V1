# Stage 0: Base image with common system dependencies and global Python setup
FROM ghcr.io/runpod-workers/worker-comfyui:cuda-12.1 AS base

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_PREFER_BINARY=1
ENV PYTHONUNBUFFERED=1
ENV CMAKE_BUILD_PARALLEL_LEVEL=8

# Install Python 3.10, system tools, and dependencies
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
    && rm -rf /var/lib/apt/lists/*

# Link python3 and pip3 to consistent names
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

# Install git
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN git --version

# ----------------------------------------
# Stage 1: Installer
FROM base AS installer

# Install uv and create virtual environment
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

# Clone ComfyUI
WORKDIR /comfyui
RUN git clone https://github.com/comfyanonymous/ComfyUI.git .

# Install ComfyUI core requirements
RUN uv pip install --system -r requirements.txt

# Add your own requirements
COPY requirements.txt /tmp/user_requirements.txt

# Install all remaining dependencies including xformers
RUN uv pip install --system \
    -r /tmp/user_requirements.txt \
    xformers==0.0.22.post7 \
    --index-url https://download.pytorch.org/whl/cu121 \
    --extra-index-url https://pypi.org/simple/

# Ensure runpod is installed in the venv using uv (no pip calls!)
RUN uv pip install --system --upgrade 'runpod>=1.7.12'

# Install custom nodes
RUN git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git custom_nodes/XLabs-AI && \
    if [ -f custom_nodes/XLabs-AI/requirements.txt ]; then \
        uv pip install --system -r custom_nodes/XLabs-AI/requirements.txt; \
    fi

RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git custom_nodes/comfyui-kjnodes && \
    if [ -f custom_nodes/comfyui-kjnodes/requirements.txt ]; then \
        uv pip install --system -r custom_nodes/comfyui-kjnodes/requirements.txt; \
    fi

# ----------------------------------------
# Stage 2: Downloader - Download models
FROM installer AS downloader

ARG HUGGINGFACE_ACCESS_TOKEN
ARG MODEL_TYPE=flux1-dev-fp8

WORKDIR /comfyui
RUN mkdir -p models/checkpoints models/vae models/unet models/clip models/controlnet models/text_encoders

# Download model files
RUN wget -O models/text_encoders/clip_l.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors
RUN wget -O models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors
RUN wget -O models/checkpoints/flux1-dev-kontext_fp8_scaled.safetensors https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors
RUN wget -O models/vae/ae.safetensors https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors
RUN wget -O models/controlnet/flux-depth-controlnet-v3.safetensors https://huggingface.co/XLabs-AI/flux-controlnet-depth-v3/resolve/main/flux-depth-controlnet-v3.safetensors

# ----------------------------------------
# Stage 3: Final runtime image
FROM base AS final

ENV PATH="/opt/venv/bin:${PATH}"

# Copy venv and ComfyUI install
COPY --from=installer /opt/venv /opt/venv
COPY --from=installer /comfyui /comfyui

# Copy models
COPY --from=downloader /comfyui/models /comfyui/models

# Setup worker application
WORKDIR /workspace/worker
RUN mkdir -p /workspace/worker/src
ADD src/start.sh /workspace/worker/start.sh
ADD src/handler.py /workspace/worker/src/handler.py
RUN chmod +x /workspace/worker/start.sh

# Copy workflows
COPY workflows/ /workspace/worker/workflows/

# Copy helper scripts
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-node-install /usr/local/bin/comfy-manager-set-mode

ENV COMFYUI_HOST=127.0.0.1
ENV COMFYUI_PORT=8080

EXPOSE 8080
CMD ["/workspace/worker/start.sh"]
