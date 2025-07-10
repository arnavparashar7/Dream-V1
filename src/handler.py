import runpod
from runpod.serverless.utils import rp_download, rp_cleanup
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
from io import BytesIO
import websocket
import uuid
import tempfile
import socket
import traceback
import requests
import pathlib

# Cloudflare Images Configuration (Environment Variables)
CF_IMAGES_ACCOUNT_ID = os.environ.get("CF_IMAGES_ACCOUNT_ID")
CF_IMAGES_API_TOKEN = os.environ.get("CF_IMAGES_API_TOKEN")

# Global variables for ComfyUI API
COMFYUI_HOST = os.environ.get("COMFYUI_HOST", "127.0.0.1")
COMFYUI_PORT = os.environ.get("COMFYUI_PORT", "8080")
COMFYUI_URL = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}"
WS_URL = f"ws://{COMFYUI_HOST}:{COMFYUI_PORT}/ws?clientId="

def upload_to_cloudflare_images(file_path: str) -> str:
    """
    Uploads a local file to Cloudflare Images.
    Returns the public URL of the uploaded image.
    """
    if not CF_IMAGES_ACCOUNT_ID or not CF_IMAGES_API_TOKEN:
        print("Cloudflare Images credentials missing. Cannot upload image.")
        return None # Indicate failure to upload

    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_IMAGES_ACCOUNT_ID}/images/v1"
    headers = {"Authorization": f"Bearer {CF_IMAGES_API_TOKEN}"}

    with pathlib.Path(file_path).open("rb") as img:
        files = {"file": img}
        data  = {
            "requireSignedURLs": "false",     # Set to "true" if you want signed URLs
            "metadata": json.dumps({"src": os.path.basename(file_path)})
        }
        try:
            r = requests.post(url, headers=headers, files=files, data=data)
            r.raise_for_status() # Raise an exception for HTTP errors
            response_data = r.json()
            if response_data and response_data.get("success"):
                image_id = response_data["result"]["id"]
                image_url = response_data["result"]["variants"][0] # Get the public URL
                print(f"Uploaded {os.path.basename(file_path)} to Cloudflare Images. ID: {image_id}, URL: {image_url}")
                return image_url
            else:
                print(f"Cloudflare Images upload failed: {response_data.get('errors', 'Unknown error')}")
                return None
        except requests.exceptions.RequestException as e:
            print(f"Error uploading to Cloudflare Images: {e}")
            return None
        except Exception as e:
            print(f"An unexpected error occurred during Cloudflare Images upload: {e}")
            return None

def connect_to_comfyui(client_id):
    """Establishes a WebSocket connection to ComfyUI."""
    try:
        ws = websocket.WebSocket()
        ws.connect(WS_URL + client_id)
        return ws
    except Exception as e:
        print(f"Failed to connect to ComfyUI WebSocket: {e}")
        raise

def queue_prompt(prompt, client_id, ws):
    """Queues a prompt with ComfyUI."""
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')
    try:
        response = urllib.request.urlopen(urllib.request.Request(f"{COMFYUI_URL}/prompt", data=data))
        return json.loads(response.read())
    except urllib.error.URLError as e:
        print(f"Failed to queue prompt: {e.reason}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred while queuing prompt: {e}")
        raise

def get_image(filename, subfolder, folder_type):
    """Retrieves an image from ComfyUI."""
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(f"{COMFYUI_URL}/view?{url_values}") as response:
        return response.read()

def get_history(prompt_id):
    """Retrieves prompt history from ComfyUI."""
    with urllib.request.urlopen(f"{COMFYUI_URL}/history?prompt_id={prompt_id}") as response:
        return json.loads(response.read())

def interrupt_comfyui():
    """Interrupts the ComfyUI server."""
    try:
        requests.post(f"{COMFYUI_URL}/interrupt")
        print("ComfyUI server interrupted.")
    except Exception as e:
        print(f"Failed to interrupt ComfyUI server: {e}")

def get_value_at_index(obj, index):
    """Helper to get a value from a nested list/dict."""
    for i, (key, value) in enumerate(obj.items()):
        if i == index:
            return value
    return None

def get_nodes_and_workflows_from_input(job_input):
    """
    Extracts nodes and workflow_api from the job input.
    Supports either direct 'workflow_api' or 'workflow_name' with a file lookup.
    """
    workflow_api = job_input.get("workflow_api")
    workflow_name = job_input.get("workflow_name")

    if workflow_api:
        return workflow_api

    if workflow_name:
        workflow_path = f"/workspace/worker/workflows/{workflow_name}.json"
        if not os.path.exists(workflow_path):
            raise FileNotFoundError(f"Workflow file '{workflow_path}' not found.")
        with open(workflow_path, 'r') as f:
            return json.load(f)
    raise ValueError("Neither 'workflow_api' nor 'workflow_name' provided in job input.")


def get_image_output_nodes(workflow):
    """
    Identifies and returns a list of node IDs that are 'SaveImage' nodes
    or other nodes that produce images and whose output is desired.
    """
    output_node_ids = []
    for node_id, node_data in workflow.get("nodes", {}).items():
        if node_data.get("class_type") == "SaveImage":
            output_node_ids.append(node_id)
        # Add other output node types if needed, e.g., ImageSave or custom ones
        # elif node_data.get("class_type") == "PreviewImage":
        #    output_node_ids.append(node_id)
    return output_node_ids

def wait_for_comfyui(timeout=120):
    """
    Waits for the ComfyUI server to become available.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{COMFYUI_URL}/queue", timeout=5)
            if response.status_code == 200:
                print("ComfyUI server is ready.")
                return True
        except requests.exceptions.ConnectionError:
            pass # Server not yet up
        except Exception as e:
            print(f"Error waiting for ComfyUI: {e}")
        time.sleep(1)
    print("ComfyUI server did not become ready in time.")
    return False

def handler(job):
    """
    Main handler function for RunPod.
    """
    print("worker-comfyui - Job received by handler:", job)

    ws = None # Initialize websocket to None
    try:
        job_input = job["input"]

        # 1. Download input objects
        # input_urls = job_input.get("input_urls", [])
        # if input_urls:
        #     downloaded_input_paths = rp_download.download_input_objects(input_urls)
        #     print("Downloaded inputs:", downloaded_input_paths)
        #     # You might need to adjust your workflow to use these downloaded files

        # 2. Get workflow API JSON
        workflow_api = get_nodes_and_workflows_from_input(job_input)

        # 3. Apply workflow adjustments (placeholders, etc.)
        # This is where you would iterate through your workflow_api
        # and replace placeholders like IMAGE_URL, PROMPT_TEXT, SEED, etc.
        # Example for replacing a placeholder:
        # For 'Load Image' node, replace URL
        # For 'CLIPTextEncode' node, replace text
        # For 'KSampler' node, replace seed, steps, cfg, sampler_name, scheduler

        # Example: Replacing a text prompt in a CLIPTextEncode node (assuming ID 10 is your positive prompt node)
        positive_prompt_text = job_input.get("positive_prompt", "a professional photo")
        negative_prompt_text = job_input.get("negative_prompt", "blurry, low quality, bad, ugly")
        seed = job_input.get("seed", 42)
        steps = job_input.get("steps", 20)
        cfg = job_input.get("cfg", 8.0)
        sampler_name = job_input.get("sampler_name", "euler")
        scheduler = job_input.get("scheduler", "normal")
        denoise = job_input.get("denoise", 1.0)
        image_url = job_input.get("image_url") # For LoadImageFromUrl

        # Dynamically set nodes based on workflow and input.
        # This part requires knowledge of your specific workflow structure.
        # Example: Assuming node IDs 6 and 7 are your positive and negative CLIPTextEncode nodes
        # Adjust these IDs based on your actual workflow JSON
        if workflow_api.get(str(40)) and workflow_api[str(40)].get("class_type") == "CLIPTextEncodeFlux":
            # Node 40 is CLIPTextEncodeFlux (Positive)
            workflow_api[str(40)]["inputs"]["text"] = positive_prompt_text
            workflow_api[str(40)]["inputs"]["clip"] = get_value_at_index(workflow_api, 38)[0]

        # Node 41 is CLIPTextEncodeFlux (Negative)
        if workflow_api.get(str(41)) and workflow_api[str(41)].get("class_type") == "CLIPTextEncodeFlux":
            workflow_api[str(41)]["inputs"]["text"] = negative_prompt_text
            workflow_api[str(41)]["inputs"]["clip"] = get_value_at_index(workflow_api, 39)[0]

        # KSampler node (e.g., ID 27 in Redesign.json, ID 6 in fill.json)
        # Find KSampler by class_type as ID can change per workflow
        for node_id, node_data in workflow_api.get("nodes", {}).items():
            if node_data.get("class_type") == "KSampler" or node_data.get("class_type") == "KSampler (Efficient)":
                node_data["inputs"]["seed"] = seed
                node_data["inputs"]["steps"] = steps
                node_data["inputs"]["cfg"] = cfg
                node_data["inputs"]["sampler_name"] = sampler_name
                node_data["inputs"]["scheduler"] = scheduler
                node_data["inputs"]["denoise"] = denoise
                break # Assuming only one KSampler

        # LoadImageFromUrl (e.g., ID 57 in fill.json, if you're using an input image)
        if image_url:
            for node_id, node_data in workflow_api.get("nodes", {}).items():
                if node_data.get("class_type") == "LoadImageFromUrl":
                    node_data["inputs"]["image"] = image_url
                    break

        # Convert the modified workflow_api (dict of nodes) back to a list structure if needed by ComfyUI
        # The RunPod worker expects the job["input"]["workflow_api"] to be the *actual* prompt object.
        # So, if you load from file, you loaded a dict already structured for ComfyUI.
        # If your workflow_api is just a dict of nodes, you might need to wrap it in {"prompt": nodes_dict}

        # ComfyUI expects a dictionary with a "prompt" key containing the workflow nodes.
        # If get_nodes_and_workflows_from_input returns the raw workflow dict, use it directly.
        # If it returns just the nodes, wrap it.
        # Assuming workflow_api already correctly contains the "prompt" key or is the prompt itself.
        prompt = workflow_api # ComfyUI expects this format: {"prompt": {...}}

        if not wait_for_comfyui():
            raise Exception("ComfyUI server did not become ready.")

        client_id = str(uuid.uuid4())
        ws = connect_to_comfyui(client_id)

        ws_monitor_thread = None # Placeholder for potential monitoring thread

        # Find all SaveImage nodes (or other desired output nodes)
        output_node_ids = get_image_output_nodes(workflow_api)
        if not output_node_ids:
            print("No 'SaveImage' (or other configured output) nodes found in workflow. "
                  "No images will be retrieved by the worker unless explicitly handled.")

        # Queue the prompt
        prompt_info = queue_prompt(prompt, client_id, ws)
        prompt_id = prompt_info["prompt_id"]

        output_images = []
        errors = []

        while True:
            out = ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                if message["type"] == "progress":
                    data = message["data"]
                    # print(f"Progress: {data['value']}/{data['max_value']} (Node: {data['node']})")
                elif message["type"] == "executing":
                    data = message["data"]
                    if data["node"] is None:
                        break  # Execution is done
            else:
                continue  # Binary data, likely irrelevant for progress monitoring

        # Once execution is done, retrieve images from history
        history = get_history(prompt_id)[prompt_id]

        for o in history["outputs"]:
            for node_id in output_node_ids:
                if node_id in history["outputs"]:
                    node_output = history["outputs"][node_id]
                    if "images" in node_output:
                        for image_data in node_output["images"]:
                            image = get_image(
                                image_data["filename"],
                                image_data["subfolder"],
                                image_data["type"],
                            )

                            # Save to temp file and upload to Cloudflare
                            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                                tmp_file.write(image)
                                temp_file_path = tmp_file.name

                            uploaded_url = upload_to_cloudflare_images(temp_file_path)
                            if uploaded_url:
                                output_images.append({"image_url": uploaded_url})
                            else:
                                print(f"Failed to upload image {image_data['filename']} to Cloudflare.")

                            # Clean up temp file
                            os.remove(temp_file_path)

            # Collect workflow errors if any
            if "errors" in history:
                errors.extend(history["errors"])

        # If no images were saved via SaveImage nodes but you expect some,
        # you might need to manually check for latent/image outputs from other nodes
        # based on your workflow. This handler specifically looks for SaveImage.

        print("worker-comfyui - Workflow execution finished.")

        final_result = {"images": output_images}
        if errors:
            final_result["errors"] = errors

        # 4. Clean up any downloaded inputs
        rp_cleanup.clean_up_input_objects(downloaded_input_paths if 'downloaded_input_paths' in locals() else [])

        return final_result

    except FileNotFoundError as e:
        print(f"worker-comfyui - File Error: {e}")
        return {"error": str(e)}
    except ValueError as e:
        print(f"worker-comfyui - Value Error: {e}")
        print(traceback.format_exc())
        return {"error": str(e)}
    except requests.exceptions.ConnectionError as e:
        print(f"worker-comfyui - Connection Error: {e}")
        print(traceback.format_exc())
        return {"error": f"Could not connect to ComfyUI. Is it running? {e}"}
    except websocket._exceptions.WebSocketConnectionClosedException as e:
        print(f"worker-comfyui - WebSocket Connection Closed: {e}")
        print(traceback.format_exc())
        return {"error": f"WebSocket connection to ComfyUI closed unexpectedly: {e}"}
    except Exception as e:
        print(f"worker-comfyui - Unexpected Handler Error: {e}")
        print(traceback.format_exc())
        return {"error": f"An unexpected error occurred: {e}"}
    finally:
        if ws and ws.connected:
            print(f"worker-comfyui - Closing websocket connection.")
            ws.close()

    # The original snippet you shared might have ended prematurely, leading to syntax issues.
    # The `finally` block and subsequent return statements ensure proper closure and response.
