# Use RunPod's PyTorch image with CUDA 12.8.1 and Python 3.11
FROM runpod/pytorch:2.8.0-py3.11-cuda12.1-cudnn-devel-ubuntu22.04

LABEL maintainer="you"
LABEL description="Serverless ComfyUI worker with Flux/Kontext workflows and RunPod handler."

# Avoid pip warning when root
RUN mkdir -p /etc/pip.conf.d/ && \
    echo "[global]\nroot-user-action = ignore" > /etc/pip.conf.d/pip_root_warning.conf

# Upgrade pip itself
RUN python3.11 -m pip install --no-cache-dir --upgrade pip

# ------------------------------------------------------
# Clone ComfyUI
RUN mkdir /comfyui
WORKDIR /comfyui

RUN git clone https://github.com/comfyanonymous/ComfyUI.git .

# Install ComfyUI's own requirements
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------
# Install your extra requirements (runpod, requests, websocket-client, etc.)
COPY requirements.txt /tmp/requirements.txt
RUN python3.11 -m pip install --no-cache-dir -r /tmp/requirements.txt

# ------------------------------------------------------
# Install Custom Nodes
WORKDIR /comfyui/custom_nodes

# Flux / Kontext
RUN git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git
RUN if [ -f x-flux-comfyui/requirements.txt ]; then \
      python3.11 -m pip install --no-cache-dir -r x-flux-comfyui/requirements.txt; \
    fi

# ColorMatch
RUN git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes.git
RUN if [ -f ComfyUI-KJNodes/requirements.txt ]; then \
      python3.11 -m pip install --no-cache-dir -r ComfyUI-KJNodes/requirements.txt; \
    fi

# ------------------------------------------------------
# Back to ComfyUI root
WORKDIR /comfyui

# ------------------------------------------------------
# Copy your worker code
WORKDIR /workspace/worker

RUN mkdir -p /workspace/worker/src

COPY src/start.sh /workspace/worker/start.sh
COPY src/handler.py /workspace/worker/src/handler.py

RUN chmod +x /workspace/worker/start.sh

# ------------------------------------------------------
# Copy your workflows
COPY workflows/ /workspace/worker/workflows/

# ------------------------------------------------------
# Copy your helper scripts
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode

RUN chmod +x /usr/local/bin/comfy-node-install /usr/local/bin/comfy-manager-set-mode

# ------------------------------------------------------
# Environment for ComfyUI
ENV COMFYUI_HOST=127.0.0.1
ENV COMFYUI_PORT=8080

# Expose ComfyUI port
EXPOSE 8080

# Healthcheck for ComfyUI
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# ------------------------------------------------------
# Start the worker (your start.sh handles ComfyUI + handler.py)
CMD ["/works]()
