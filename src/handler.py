import os
import json
import uuid
import time
import pathlib
import urllib.request
import urllib.parse
import tempfile
import base64
import requests
import websocket
import runpod
from runpod.serverless.utils import rp_cleanup

# ------------------------------------------------------------------
# ✅ ENVIRONMENT CONFIGURATION
# ------------------------------------------------------------------
CF_IMAGES_ACCOUNT_ID = os.environ.get("CF_IMAGES_ACCOUNT_ID")
CF_IMAGES_API_TOKEN = os.environ.get("CF_IMAGES_API_TOKEN")
COMFY_HOST = os.environ.get("COMFYUI_HOST", "127.0.0.1")
COMFY_PORT = os.environ.get("COMFYUI_PORT", "8080")
COMFY_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"
WS_URL = f"ws://{COMFY_HOST}:{COMFY_PORT}/ws?clientId="

# ------------------------------------------------------------------
# ✅ CLOUDFLARE IMAGE UPLOAD
# ------------------------------------------------------------------
def upload_to_cloudflare_images(file_path):
    if not CF_IMAGES_ACCOUNT_ID or not CF_IMAGES_API_TOKEN:
        print("Cloudflare ENV missing.")
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
            r = requests.post(url, headers=headers, files=files, data=data)
            r.raise_for_status()
            resp = r.json()
            if resp.get("success"):
                return resp["result"]["variants"][0]
        except Exception as e:
            print(f"Cloudflare upload failed: {e}")
    return None

# ------------------------------------------------------------------
# ✅ WORKFLOW HANDLING + NODE INJECTION
# ------------------------------------------------------------------
def load_workflow(mode):
    filename = f"/workspace/worker/workflows/{'Redesign' if mode == 'redesign' else 'fill'}.json"
    with open(filename, 'r') as f:
        return json.load(f)

def inject_inputs(workflow, job_input, mode):
    prompt = job_input.get("positive_prompt", "a professional photo")
    image_url = job_input.get("image_url", None)

    if mode == "fill":
        if "43" in workflow:
            workflow["43"]["inputs"][0] = prompt
        if "57" in workflow:
            workflow["57"]["inputs"][0] = image_url
    else:
        if "63" in workflow:
            workflow["63"]["inputs"][0] = prompt
        if "15" in workflow:
            workflow["15"]["inputs"][0] = "remove all the furniture like sofas, tables, plants, lights, fireplace, paintings, curtains and carpet"
    return workflow

def get_output_node(mode):
    return "42" if mode == "fill" else "54"

# ------------------------------------------------------------------
# ✅ COMFYUI CONNECTION HELPERS
# ------------------------------------------------------------------
def wait_for_comfyui(timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if requests.get(f"{COMFY_URL}/queue").status_code == 200:
                return True
        except:
            pass
        time.sleep(1)
    return False

def connect_ws(client_id):
    ws = websocket.WebSocket()
    ws.connect(WS_URL + client_id)
    return ws

def queue_prompt(workflow, client_id):
    data = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    req = urllib.request.Request(f"{COMFY_URL}/prompt", data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def get_history(prompt_id):
    with urllib.request.urlopen(f"{COMFY_URL}/history?prompt_id={prompt_id}") as resp:
        return json.loads(resp.read())

def get_image(filename, subfolder, folder_type):
    params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": folder_type})
    with urllib.request.urlopen(f"{COMFY_URL}/view?{params}") as resp:
        return resp.read()

# ------------------------------------------------------------------
# ✅ MAIN HANDLER
# ------------------------------------------------------------------
def handler(job):
    job_input = job.get("input", {})
    mode = job_input.get("mode", "fill").lower()

    try:
        if not wait_for_comfyui():
            return {"error": "ComfyUI is not reachable."}

        workflow = load_workflow(mode)
        workflow = inject_inputs(workflow, job_input, mode)
        output_node_id = get_output_node(mode)

        client_id = str(uuid.uuid4())
        ws = connect_ws(client_id)
        prompt_info = queue_prompt(workflow, client_id)
        prompt_id = prompt_info["prompt_id"]

        while True:
            msg = json.loads(ws.recv())
            if msg.get("type") == "executing" and msg.get("data", {}).get("node") is None:
                break

        history = get_history(prompt_id).get(prompt_id, {})
        output_images = []

        if output_node_id in history.get("outputs", {}):
            node_output = history["outputs"][output_node_id]
            for img_data in node_output.get("images", []):
                raw_img = get_image(img_data["filename"], img_data["subfolder"], img_data["type"])

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(raw_img)
                    path = tmp.name

                url = upload_to_cloudflare_images(path)
                if url:
                    output_images.append({"image_url": url})
                else:
                    b64 = base64.b64encode(raw_img).decode()
                    output_images.append({"base64": b64})

                os.remove(path)

        return {"images": output_images}

    except Exception as e:
        return {"error": str(e)}

# ------------------------------------------------------------------
# ✅ START HANDLER
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("worker-comfyui - Handler starting...")
    runpod.serverless.start({"handler": handler})
