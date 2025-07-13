import runpod
import os
import json
import requests
import websocket
import uuid
import time
import urllib.parse
import traceback
import pathlib
import tempfile
import base64

# ------------------------------------------------------------------------------
# ENV VARS
# ------------------------------------------------------------------------------

COMFY_HOST = os.environ.get("COMFYUI_HOST", "127.0.0.1")
COMFY_PORT = os.environ.get("COMFYUI_PORT", "8080")
COMFY_HTTP = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_WS = f"ws://{COMFY_HOST}:{COMFY_PORT}/ws?clientId="

CF_IMAGES_ACCOUNT_ID = os.environ.get("CF_IMAGES_ACCOUNT_ID")
CF_IMAGES_API_TOKEN = os.environ.get("CF_IMAGES_API_TOKEN")

# ------------------------------------------------------------------------------
# Cloudflare Images uploader
# ------------------------------------------------------------------------------

def upload_to_cloudflare_images(file_path: str) -> str:
    if not CF_IMAGES_ACCOUNT_ID or not CF_IMAGES_API_TOKEN:
        print("Cloudflare Images credentials missing. Skipping upload.")
        return None

    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_IMAGES_ACCOUNT_ID}/images/v1"
    headers = {"Authorization": f"Bearer {CF_IMAGES_API_TOKEN}"}

    with pathlib.Path(file_path).open("rb") as img:
        files = {"file": img}
        data = {
            "requireSignedURLs": "false",
            "metadata": json.dumps({"src": os.path.basename(file_path)})
        }
        try:
            response = requests.post(url, headers=headers, files=files, data=data)
            response.raise_for_status()
            resp_data = response.json()
            if resp_data.get("success"):
                return resp_data["result"]["variants"][0]
            else:
                print(f"Cloudflare upload error: {resp_data.get('errors')}")
                return None
        except Exception as e:
            print(f"Error uploading to Cloudflare Images: {e}")
            return None

# ------------------------------------------------------------------------------
# Helper: pick workflow file
# ------------------------------------------------------------------------------

def load_workflow_from_file(name):
    workflow_map = {
        "fill": "fill.json",
        "redesign": "Redesign.json"
    }
    # default to redesign if invalid
    selected_file = workflow_map.get(name.lower(), "Redesign.json")
    path = f"/workspace/worker/workflows/{selected_file}"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Workflow file not found: {path}")
    with open(path, 'r') as f:
        return json.load(f)

# ------------------------------------------------------------------------------
# Helper: wait for ComfyUI readiness
# ------------------------------------------------------------------------------

def wait_for_comfyui(timeout=120):
    print(f"Waiting for ComfyUI at {COMFY_HTTP}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_HTTP}/queue", timeout=5)
            if r.status_code == 200:
                print("ComfyUI is ready.")
                return True
        except requests.exceptions.ConnectionError:
            pass
        except Exception as e:
            print(f"Error waiting for ComfyUI: {e}")
        time.sleep(1)
    print("Timed out waiting for ComfyUI.")
    return False

# ------------------------------------------------------------------------------
# Helper: connect websocket
# ------------------------------------------------------------------------------

def connect_to_comfyui_ws(client_id):
    try:
        ws = websocket.WebSocket()
        ws.connect(COMFY_WS + client_id, timeout=10)
        return ws
    except Exception as e:
        print(f"Error connecting WebSocket: {e}")
        raise

# ------------------------------------------------------------------------------
# Helpers for talking to ComfyUI
# ------------------------------------------------------------------------------

def queue_prompt(prompt, client_id):
    payload = {"prompt": prompt, "client_id": client_id}
    try:
        resp = requests.post(f"{COMFY_HTTP}/prompt", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error queuing prompt: {e}")
        raise

def get_history(prompt_id):
    resp = requests.get(f"{COMFY_HTTP}/history/{prompt_id}", timeout=60)
    resp.raise_for_status()
    return resp.json()

def get_image(filename, subfolder, folder_type):
    params = urllib.parse.urlencode({
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type
    })
    resp = requests.get(f"{COMFY_HTTP}/view?{params}", timeout=60)
    resp.raise_for_status()
    return resp.content

# ------------------------------------------------------------------------------
# Helper: inject variables into workflow
# ------------------------------------------------------------------------------

def apply_inputs_to_workflow(workflow, job_input):
    """
    You can add more variables here as needed in future.
    For now we handle positive/negative prompts and image_url
    """
    positive = job_input.get("positive_prompt", "A professional photo")
    negative = job_input.get("negative_prompt", "bad, blurry")
    image_url = job_input.get("image_url", None)

    for node_id, node in workflow.get("nodes", {}).items():
        if node.get("class_type") in {"CLIPTextEncodeFlux", "CLIPTextEncode"}:
            if "positive" in node.get("inputs", {}):
                node["inputs"]["positive"] = positive
            if "negative" in node.get("inputs", {}):
                node["inputs"]["negative"] = negative
            if "text" in node.get("inputs", {}):
                # Older format
                if "positive" in node["inputs"]:
                    node["inputs"]["text"] = positive
                else:
                    node["inputs"]["text"] = negative

        if image_url and node.get("class_type") == "LoadImageFromUrl":
            node["inputs"]["image"] = image_url

    return workflow

# ------------------------------------------------------------------------------
# MAIN HANDLER
# ------------------------------------------------------------------------------

def handler(job):
    print("Handler invoked.")

    job_input = job.get("input", {})
    workflow_choice = job_input.get("workflow", "redesign")

    try:
        if not wait_for_comfyui():
            return {"error": "ComfyUI server is not reachable."}

        print(f"Loading workflow for choice: {workflow_choice}")
        workflow_data = load_workflow_from_file(workflow_choice)

        # Inject prompts etc.
        workflow_data = apply_inputs_to_workflow(workflow_data, job_input)

        client_id = str(uuid.uuid4())
        ws = connect_to_comfyui_ws(client_id)

        # Queue the prompt
        prompt_resp = queue_prompt(workflow_data, client_id)
        prompt_id = prompt_resp.get("prompt_id")
        if not prompt_id:
            raise Exception("No prompt_id returned from ComfyUI.")

        # Wait for execution to finish
        print(f"Prompt queued. ID: {prompt_id}")
        while True:
            msg = ws.recv()
            if isinstance(msg, str):
                message = json.loads(msg)
                if message.get("type") == "executing":
                    if message.get("data", {}).get("node") is None:
                        print("Execution complete.")
                        break
            else:
                continue

        # Retrieve history
        history_data = get_history(prompt_id).get(prompt_id, {})
        outputs = history_data.get("outputs", {})

        image_urls = []
        for node_output in outputs.values():
            for image_info in node_output.get("images", []):
                filename = image_info.get("filename")
                subfolder = image_info.get("subfolder")
                img_type = image_info.get("type")

                if not filename or img_type == "temp":
                    continue

                img_bytes = get_image(filename, subfolder, img_type)
                if not img_bytes:
                    continue

                # Save temp
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                    tmp_file.write(img_bytes)
                    tmp_path = tmp_file.name

                # Upload to Cloudflare
                uploaded_url = upload_to_cloudflare_images(tmp_path)
                os.remove(tmp_path)

                if uploaded_url:
                    image_urls.append(uploaded_url)
                else:
                    # Fallback: Base64 if CF upload fails
                    b64_image = base64.b64encode(img_bytes).decode("utf-8")
                    image_urls.append(f"data:image/png;base64,{b64_image}")

        return {"images": image_urls}

    except Exception as e:
        print(f"Handler error: {e}")
        traceback.print_exc()
        return {"error": str(e)}

# ------------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting RunPod handler...")
    runpod.serverless.start({"handler": handler})
