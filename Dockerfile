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
# Set default model type if none is provided
ARG MODEL_TYPE=flux1-dev-fp8

# Change working directory to ComfyUI
WORKDIR /comfyui

# Create necessary directories upfront
RUN mkdir -p models/checkpoints models/vae models/unet models/clip

# Download checkpoints/vae/unet/clip models to include in image based on model type
# --- Model Downloads ---
# CLIP Text Encoders
RUN wget -O /workspace/ComfyUI/models/text_encoders/clip_l.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors
RUN wget -O /workspace/ComfyUI/models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors

# UNET/Diffusion Model
RUN wget -O /workspace/ComfyUI/models/checkpoints/flux1-dev-kontext_fp8_scaled.safetensors https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors

# VAE
RUN wget -O /workspace/ComfyUI/models/vae/ae.safetensors https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors

# Flux ControlNet Model
RUN wget -O /workspace/ComfyUI/models/controlnet/flux-depth-controlnet-v3.safetensors https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/controlnet/flux-depth-controlnet-v3.safetensors

# Add any other models if necessary, ensure they go into the correct ComfyUI model sub-folder.
# Example: RUN wget -O /workspace/ComfyUI/models/loras/my_lora.safetensors https://example.com/my_lora.safetensors

# Stage 3: Final image
FROM base AS final

# Copy models from stage 2 to the final image
COPY --from=downloader /comfyui/models /comfyui/models

# Copy workflows
COPY workflows/ /workspace/worker/workflows/

# --- Custom Nodes Installation ---
# Install Flux and Kontext nodes (these might already be covered by RunPod's base image, but good to ensure)
RUN git clone https://github.com/XLabs-AI/x-flux-comfyui.git custom_nodes/XLabs-AI # Contains some base Flux nodes
# For FluxGuidance, ReferenceLatent, DualCLIPLoader, DifferentialDiffusion, FluxKontextImageScale, CLIPTextEncodeFlux
RUN git clone git clone https://github.com/Light-x02/ComfyUI-FluxSettingsNode.git custom_nodes/ComfyUI-Flux-Nodes && \
    pip install -r custom_nodes/ComfyUI-Flux-Nodes/requirements.txt

# For ColorMatch (from comfyui-kjnodes)
RUN git clone https://github.com/kijai/ComfyUI-KJNodes.git custom_nodes/comfyui-kjnodes && \
    pip install -r custom_nodes/comfyui-kjnodes/requirements.txt

# Add any other custom nodes you might have, using the same pattern:
# RUN git clone <your_custom_node_repo_url> custom_nodes/<your_custom_node_folder_name> && \
#     pip install -r custom_nodes/<your_custom_node_folder_name>/requirements.txt

RUN pip install requests