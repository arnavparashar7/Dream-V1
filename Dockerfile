# Use a NVIDIA CUDA base image for GPU support
FROM nvcr.io/nvidia/cuda:12.1.1-devel-ubuntu22.04 AS base

# Install necessary system packages for Python, Git, and others
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3.10-distutils \
    python3.10-venv \
    python3-pip \
    git \
    wget \
    curl \
    unzip \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Link python3 to python3.10 for consistency
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1

# Stage 1: Installer - Set up ComfyUI and install all dependencies
FROM base AS installer

# Set Python build options for uv to install into the system Python
ENV UV_SYSTEM_PYTHON=1
ENV UV_PYTHON_INSTALL_NATIVE_LIBS=1

# Copy the custom requirements.txt from the build context (repo root)
# to a known, distinct location inside the image (e.g., /tmp/user_requirements.txt)
# This ensures YOUR requirements.txt (with 'runpod') is available
COPY requirements.txt /tmp/user_requirements.txt

# Change to /comfyui directory for ComfyUI installation
WORKDIR /comfyui

# Clone ComfyUI repository
RUN git clone https://github.com/comfyanonymous/ComfyUI.git .

# Install uv for fast dependency management and package installation
RUN python3 -m pip install uv

# Install ALL Python dependencies:
# - ComfyUI's original requirements.txt
# - Your custom requirements.txt (which includes 'runpod', 'websocket-client', 'requests')
# - Specific xformers version (crucial for performance with PyTorch)
# - Specify PyTorch CUDA 12.1 wheel URL and general PyPI for other packages
RUN uv pip install --system \
    -r requirements.txt \
    -r /tmp/user_requirements.txt \
    xformers==0.0.22.post7 \
    --index-url https://download.pytorch.org/whl/cu121 \
    --extra-index-url https://pypi.org/simple/

# --- Custom Nodes Installation ---
# These custom nodes will be installed directly into the ComfyUI folder from this installer stage.

# XLabs-AI/x-flux-comfyui contains core Flux nodes like DualCLIPLoader, DifferentialDiffusion, FluxGuidance, FluxControlNet etc.
RUN git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git custom_nodes/XLabs-AI && \
    pip install -r custom_nodes/XLabs-AI/requirements.txt # Ensure requirements for x-flux-comfyui are installed

# For ColorMatch (from comfyui-kjnodes)
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git custom_nodes/comfyui-kjnodes && \
    pip install -r custom_nodes/comfyui-kjnodes/requirements.txt

# Stage 2: Final image - Copy only necessary files from installer stage
FROM base

# Set the working directory for the worker
WORKDIR /workspace/worker

# Create the src directory inside the worker directory for handler.py
RUN mkdir -p /workspace/worker/src

# Copy handler.py and other worker files from the build context (assuming they are in src/)
COPY src/ /workspace/worker/src/

# Copy workflows
COPY workflows/ /workspace/worker/workflows/

# Copy scripts (e.g., for model downloading)
COPY scripts/ /workspace/worker/scripts/

# Copy ComfyUI and its installed dependencies and custom nodes from the installer stage
COPY --from=installer /comfyui /comfyui

# Set environment variables for ComfyUI access (if needed by handler.py)
ENV COMFYUI_HOST=127.0.0.1
ENV COMFYUI_PORT=8080

# Expose the ComfyUI port (if you want to access it directly, typically not for worker)
EXPOSE 8080

# Set the entrypoint for the worker
# This will execute the handler.py script when the container starts
ENTRYPOINT ["python3", "-u", "/workspace/worker/src/handler.py"]
