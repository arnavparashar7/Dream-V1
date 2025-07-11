# Stage 1: Installer - Set up ComfyUI and install all dependencies
FROM base AS installer

# Install uv (latest) using official installer and create isolated venv
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

# Use the virtual environment for all subsequent commands in this stage
ENV PATH="/opt/venv/bin:${PATH}"

# Clone ComfyUI directly and install its requirements
WORKDIR /comfyui
RUN git clone https://github.com/comfyanonymous/ComfyUI.git .

# Install ComfyUI's core requirements
RUN uv pip install --system -r requirements.txt

# Copy the custom requirements.txt from the build context (repo root)
# to a known, distinct location inside the image (e.g., /tmp/user_requirements.txt)
COPY requirements.txt /tmp/user_requirements.txt

# Install Python runtime dependencies for the handler into the venv
# This includes 'runpod', 'requests', 'websocket-client'
# This RUN command also handles xformers and PyTorch-related dependencies
RUN uv pip install --system \
    -r /tmp/user_requirements.txt \
    xformers==0.0.22.post7 \
    --index-url https://download.pytorch.org/whl/cu121 \
    --extra-index-url https://pypi.org/simple/

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
