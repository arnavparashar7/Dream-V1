import runpod
import os
import json
import requests
import tempfile
import websocket
import uuid
import traceback
import urllib.parse
import base64
from io import BytesIO
import pathlib

# -----------------------------------------------------------------------------
# ENVIRONMENT
# -----------------------------------------------------------------------------
CF_IMAGES_ACCOUNT_ID = os.environ.get("CF_IMAGES_ACCOUNT_ID")
CF_IMAGES_API_TOKEN = os.environ.get("CF_IMAGES_API_TOKEN")
COMFY_HOST = os.environ.get("COMFYUI_HOST", "127.0.0.1")
COMFY_PORT = os.environ.get("COMFYUI_PORT", "8080")
COMFY_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"
WS_URL = f"ws://{COMFY_HOST}:{COMFY_PORT}/ws?clientId="

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

def upload_to_cloudflare_images(file_path):
    if not CF_IMAGES_ACCOUNT_ID or not CF_IMAGES_API_TOKEN:
        print("⚠️ No Cloudflare Images credentials configured.")
        return None

    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_IMAGES_ACCOUNT_ID}/images/v1"
    headers = {"Authorization": f"Bearer {CF_IMAGES_API_TOKEN}"}
    with pathlib.Path(file_path).open("rb") as img:
        files = {"file": img}
        data = {"requireSignedURLs": "false"}
        try:
            response = requests.post(url, headers=headers, files=files, data=data)
            response.raise_for_status()
            data = response.json()
            if data.get("success"):
                return data["result"]["variants"][0]
            else:
                print(f"Cloudflare upload error: {data.get('errors')}")
        except Exception as e:
            print(f"Cloudflare upload exception: {e}")
    return None

def check_comfy_ready(timeout=120):
    import time
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_URL}/queue", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False

def connect_ws(client_id):
    ws = websocket.WebSocket()
    ws.connect(WS_URL + client_id)
    return ws

def queue_prompt(prompt, client_id):
    payload = json.dumps({"prompt": prompt, "client_id": client_id}).encode("utf-8")
    r = requests.post(f"{COMFY_URL}/prompt", data=payload)
    r.raise_for_status()
    return r.json()["prompt_id"]

def get_history(prompt_id):
    r = requests.get(f"{COMFY_URL}/history/{prompt_id}")
    r.raise_for_status()
    return r.json().get(prompt_id)

def get_image(filename, subfolder, folder_type):
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url = f"{COMFY_URL}/view?" + urllib.parse.urlencode(data)
    r = requests.get(url)
    r.raise_for_status()
    return r.content

def load_workflow(workflow_name):
    path = f"/workspace/worker/workflows/{workflow_name}.json"
    with open(path, 'r') as f:
        return json.load(f)

def get_output_nodes(workflow):
    save_nodes = []
    for node_id, node_data in workflow.get("nodes", {}).items():
        if node_data.get("class_type") == "SaveImage":
            save_nodes.append(node_id)
    return save_nodes

# -----------------------------------------------------------------------------
# NODE INJECTION (YOUR CUSTOM RULES)
# -----------------------------------------------------------------------------

def inject_inputs_fill(workflow, user_prompt, image_url):
    """
    FILL workflow:
    - Positive Prompt: Node 43[0]
    - Image URL: Node 57[0]
    """
    if "43" in workflow.get("nodes", {}):
        workflow["nodes"]["43"]["inputs"]["text"] = user_prompt
        print(f"✅ Injected user prompt into Node 43")
    if "57" in workflow.get("nodes", {}):
        workflow["nodes"]["57"]["inputs"]["image"] = image_url
        print(f"✅ Injected image URL into Node 57")

def inject_inputs_redesign(workflow, user_prompt):
    """
    REDESIGN workflow:
    - User Positive Prompt: Node 63[0]
    - Fixed furniture-removal prompt: Node 15[0]
    """
    if "63" in workflow.get("nodes", {}):
        workflow["nodes"]["63"]["inputs"]["text"] = user_prompt
        print(f"✅ Injected user prompt into Node 63")

    if "15" in workflow.get("nodes", {}):
        workflow["nodes"]["15"]["inputs"]["text"] = "remove all the furniture like sofas, tables, plants, lights, fireplace, paintings, curtains and carpet"
        print(f"✅ Hard-coded removal prompt into Node 15")

# -----------------------------------------------------------------------------
# MAIN HANDLER
# -----------------------------------------------------------------------------

def handler(job):
    job_input = job.get("input", {})
    print(f"worker-comfyui - Received job input: {json.dumps(job_input)}")

    # Select workflow type
    workflow_type = job_input.get("workflow", "fill").strip().lower()
    if workflow_type == "redesign":
        workflow_file = "Redesign"
    else:
        workflow_file = "fill"

    print(f"worker-comfyui - Selected workflow: {workflow_file}")

    # Load workflow file
    try:
        workflow = load_workflow(workflow_file)
    except FileNotFoundError:
        return {"error": f"Workflow file '{workflow_file}.json' not found on server."}

    # Extract user inputs
    user_prompt = job_input.get("positive_prompt", "A professional interior design photo")
    image_url = job_input.get("image_url", "")

    # Apply user inputs
    if workflow_file.lower() == "fill":
        inject_inputs_fill(workflow, user_prompt, image_url)
    else:
        inject_inputs_redesign(workflow, user_prompt)

    # Wait for ComfyUI server
    if not check_comfy_ready():
        return {"error": "ComfyUI server did not become ready in time."}

    # WebSocket setup
    client_id = str(uuid.uuid4())
    ws = None
    output_images = []
    errors = []

    try:
        ws = connect_ws(client_id)
        prompt_id = queue_prompt(workflow, client_id)
        print(f"worker-comfyui - Queued prompt ID: {prompt_id}")

        while True:
            message = ws.recv()
            if isinstance(message, str):
                msg = json.loads(message)
                if msg.get("type") == "executing" and msg["data"].get("node") is None:
                    print("worker-comfyui - Execution complete.")
                    break
            else:
                continue

        history = get_history(prompt_id)
        if not history:
            return {"error": f"No history found for prompt_id {prompt_id}"}

        # Find outputs
        output_nodes = get_output_nodes(workflow)
        for node_id in output_nodes:
            node_output = history.get("outputs", {}).get(node_id, {})
            if "images" in node_output:
                for img_data in node_output["images"]:
                    img_bytes = get_image(img_data["filename"], img_data["subfolder"], img_data["type"])
                    # Upload to Cloudflare
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                        tmp_file.write(img_bytes)
                        temp_file_path = tmp_file.name
                    uploaded_url = upload_to_cloudflare_images(temp_file_path)
                    os.remove(temp_file_path)

                    if uploaded_url:
                        output_images.append({"url": uploaded_url})
                    else:
                        base64_data = base64.b64encode(img_bytes).decode("utf-8")
                        output_images.append({"base64": base64_data})

    except Exception as e:
        print(f"worker-comfyui - Error: {e}")
        print(traceback.format_exc())
        errors.append(str(e))
    finally:
        if ws and ws.connected:
            ws.close()

    if not output_images and errors:
        return {"error": "Job failed", "details": errors}

    print(f"worker-comfyui - Completed job with {len(output_images)} images.")
    return {"images": output_images, "errors": errors}

# -----------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("worker-comfyui - Starting handler...")
    runpod.serverless.start({"handler": handler})
