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
            
            result = r.json()["result"]
            # Cloudflare Images returns multiple variants, typically the first is 'public'
            public_url = result["variants"][0] # This will be the direct image delivery URL
            print(f"Uploaded {file_path} to Cloudflare Images: {public_url}")
            return public_url
        except requests.exceptions.RequestException as e:
            print(f"Error uploading to Cloudflare Images: {e}")
            print(f"Response: {r.text if 'r' in locals() else 'No response'}")
            return None
        except KeyError as e:
            print(f"Cloudflare Images response missing expected key: {e}")
            print(f"Full response: {r.json() if 'r' in locals() else 'No response'}")
            return None

# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = 50
# Maximum number of API check attempts
COMFY_API_AVAILABLE_MAX_RETRIES = 500
# Websocket reconnection behaviour (can be overridden through environment variables)
# NOTE: more attempts and diagnostics improve debuggability whenever ComfyUI crashes mid-job.
#   • WEBSOCKET_RECONNECT_ATTEMPTS sets how many times we will try to reconnect.
#   • WEBSOCKET_RECONNECT_DELAY_S sets the sleep in seconds between attempts.
#
# If the respective env-vars are not supplied we fall back to sensible defaults ("5" and "3").
WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))

# Extra verbose websocket trace logs (set WEBSOCKET_TRACE=true to enable)
if os.environ.get("WEBSOCKET_TRACE", "false").lower() == "true":
    # This prints low-level frame information to stdout which is invaluable for diagnosing
    # protocol errors but can be noisy in production – therefore gated behind an env-var.
    websocket.enableTrace(True)

# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"
# Enforce a clean state after each job is done
# see https://docs.runpod.io/docs/handler-additional-controls#refresh-worker
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Helper: quick reachability probe of ComfyUI HTTP endpoint (port 8188)
# ---------------------------------------------------------------------------

def _comfy_server_status():
    """Return a dictionary with basic reachability info for the ComfyUI HTTP server."""
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {
            "reachable": resp.status_code == 200,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _attempt_websocket_reconnect(ws_url, max_attempts, delay_s, initial_error):
    """
    Attempts to reconnect to the WebSocket server after a disconnect.

    Args:
        ws_url (str): The WebSocket URL (including client_id).
        max_attempts (int): Maximum number of reconnection attempts.
        delay_s (int): Delay in seconds between attempts.
        initial_error (Exception): The error that triggered the reconnect attempt.

    Returns:
        websocket.WebSocket: The newly connected WebSocket object.

    Raises:
        websocket.WebSocketConnectionClosedException: If reconnection fails after all attempts.
    """
    print(
        f"worker-comfyui - Websocket connection closed unexpectedly: {initial_error}. Attempting to reconnect..."
    )
    last_reconnect_error = initial_error
    for attempt in range(max_attempts):
        # Log current server status before each reconnect attempt so that we can
        # see whether ComfyUI is still alive (HTTP port 8188 responding) even if
        # the websocket dropped. This is extremely useful to differentiate
        # between a network glitch and an outright ComfyUI crash/OOM-kill.
        srv_status = _comfy_server_status()
        if not srv_status["reachable"]:
            # If ComfyUI itself is down there is no point in retrying the websocket –
            # bail out immediately so the caller gets a clear "ComfyUI crashed" error.
            print(
                f"worker-comfyui - ComfyUI HTTP unreachable – aborting websocket reconnect: {srv_status.get('error', 'status '+str(srv_status.get('status_code')))}"
            )
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )

        # Otherwise we proceed with reconnect attempts while server is up
        print(
            f"worker-comfyui - Reconnect attempt {attempt + 1}/{max_attempts}... (ComfyUI HTTP reachable, status {srv_status.get('status_code')})"
        )
        try:
            # Need to create a new socket object for reconnect
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)  # Use existing ws_url
            print(f"worker-comfyui - Websocket reconnected successfully.")
            return new_ws  # Return the new connected socket
        except (
            websocket.WebSocketException,
            ConnectionRefusedError,
            socket.timeout,
            OSError,
        ) as reconn_err:
            last_reconnect_error = reconn_err
            print(
                f"worker-comfyui - Reconnect attempt {attempt + 1} failed: {reconn_err}"
            )
            if attempt < max_attempts - 1:
                print(
                    f"worker-comfyui - Waiting {delay_s} seconds before next attempt..."
                )
                time.sleep(delay_s)
            else:
                print(f"worker-comfyui - Max reconnection attempts reached.")

    # If loop completes without returning, raise an exception
    print("worker-comfyui - Failed to reconnect websocket after connection closed.")
    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_reconnect_error}"
    )


def validate_input(job_input):
    """
    Validates the input for the handler function.

    Args:
        job_input (dict): The input data to validate.

    Returns:
        tuple: A tuple containing the validated data and an error message, if any.
               The structure is (validated_data, error_message).
    """
    # Validate if job_input is provided
    if job_input is None:
        return None, "Please provide input"

    # Check if input is a string and try to parse it as JSON
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    # Validate 'workflow' in input
    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    # Validate 'images' in input, if provided
    images = job_input.get("images")
    if images is not None:
        if not isinstance(images, list) or not all(
            "name" in image and "image" in image for image in images
        ):
            return (
                None,
                "'images' must be a list of objects with 'name' and 'image' keys",
            )

    # Return validated data and no error
    return {"workflow": workflow, "images": images}, None


def check_server(url, retries=500, delay=50):
    """
    Check if a server is reachable via HTTP GET request

    Args:
    - url (str): The URL to check
    - retries (int, optional): The number of times to attempt connecting to the server. Default is 50
    - delay (int, optional): The time in milliseconds to wait between retries. Default is 500

    Returns:
    bool: True if the server is reachable within the given number of retries, otherwise False
    """

    print(f"worker-comfyui - Checking API server at {url}...")
    for i in range(retries):
        try:
            response = requests.get(url, timeout=5)

            # If the response status code is 200, the server is up and running
            if response.status_code == 200:
                print(f"worker-comfyui - API is reachable")
                return True
        except requests.Timeout:
            pass
        except requests.RequestException as e:
            pass

        # Wait for the specified delay before retrying
        time.sleep(delay / 1000)

    print(
        f"worker-comfyui - Failed to connect to server at {url} after {retries} attempts."
    )
    return False


def upload_images(images):
    """
    Upload a list of base64 encoded images to the ComfyUI server using the /upload/image endpoint.

    Args:
        images (list): A list of dictionaries, each containing the 'name' of the image and the 'image' as a base64 encoded string.

    Returns:
        dict: A dictionary indicating success or error.
    """
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []

    print(f"worker-comfyui - Uploading {len(images)} image(s)...")

    for image in images:
        try:
            name = image["name"]
            image_data_uri = image["image"]  # Get the full string (might have prefix)

            # --- Strip Data URI prefix if present ---
            if "," in image_data_uri:
                # Find the comma and take everything after it
                base64_data = image_data_uri.split(",", 1)[1]
            else:
                # Assume it's already pure base64
                base64_data = image_data_uri
            # --- End strip ---

            blob = base64.b64decode(base64_data)  # Decode the cleaned data

            # Prepare the form data
            files = {
                "image": (name, BytesIO(blob), "image/png"),
                "overwrite": (None, "true"),
            }

            # POST request to upload the image
            response = requests.post(
                f"http://{COMFY_HOST}/upload/image", files=files, timeout=30
            )
            response.raise_for_status()

            responses.append(f"Successfully uploaded {name}")
            print(f"worker-comfyui - Successfully uploaded {name}")

        except base64.binascii.Error as e:
            error_msg = f"Error decoding base64 for {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.Timeout:
            error_msg = f"Timeout uploading {image.get('name', 'unknown')}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.RequestException as e:
            error_msg = f"Error uploading {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except Exception as e:
            error_msg = (
                f"Unexpected error uploading {image.get('name', 'unknown')}: {e}"
            )
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)

    if upload_errors:
        print(f"worker-comfyui - image(s) upload finished with errors")
        return {
            "status": "error",
            "message": "Some images failed to upload",
            "details": upload_errors,
        }

    print(f"worker-comfyui - image(s) upload complete")
    return {
        "status": "success",
        "message": "All images uploaded successfully",
        "details": responses,
    }


def get_available_models():
    """
    Get list of available models from ComfyUI

    Returns:
        dict: Dictionary containing available models by type
    """
    try:
        response = requests.get(f"http://{COMFY_HOST}/object_info", timeout=10)
        response.raise_for_status()
        object_info = response.json()

        # Extract available checkpoints from CheckpointLoaderSimple
        available_models = {}
        if "CheckpointLoaderSimple" in object_info:
            checkpoint_info = object_info["CheckpointLoaderSimple"]
            if "input" in checkpoint_info and "required" in checkpoint_info["input"]:
                ckpt_options = checkpoint_info["input"]["required"].get("ckpt_name")
                if ckpt_options and len(ckpt_options) > 0:
                    available_models["checkpoints"] = (
                        ckpt_options[0] if isinstance(ckpt_options[0], list) else []
                    )

        return available_models
    except Exception as e:
        print(f"worker-comfyui - Warning: Could not fetch available models: {e}")
        return {}


def queue_workflow(workflow, client_id):
    """
    Queue a workflow to be processed by ComfyUI

    Args:
        workflow (dict): A dictionary containing the workflow to be processed
        client_id (str): The client ID for the websocket connection

    Returns:
        dict: The JSON response from ComfyUI after processing the workflow

    Raises:
        ValueError: If the workflow validation fails with detailed error information
    """
    # Include client_id in the prompt payload
    payload = {"prompt": workflow, "client_id": client_id}
    data = json.dumps(payload).encode("utf-8")

    # Use requests for consistency and timeout
    headers = {"Content-Type": "application/json"}
    response = requests.post(
        f"http://{COMFY_HOST}/prompt", data=data, headers=headers, timeout=30
    )

    # Handle validation errors with detailed information
    if response.status_code == 400:
        print(f"worker-comfyui - ComfyUI returned 400. Response body: {response.text}")
        try:
            error_data = response.json()
            print(f"worker-comfyui - Parsed error data: {error_data}")

            # Try to extract meaningful error information
            error_message = "Workflow validation failed"
            error_details = []

            # ComfyUI seems to return different error formats, let's handle them all
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    error_message = error_info.get("message", error_message)
                    if error_info.get("type") == "prompt_outputs_failed_validation":
                        error_message = "Workflow validation failed"
                else:
                    error_message = str(error_info)

            # Check for node validation errors in the response
            if "node_errors" in error_data:
                for node_id, node_error in error_data["node_errors"].items():
                    if isinstance(node_error, dict):
                        for error_type, error_msg in node_error.items():
                            error_details.append(
                                f"Node {node_id} ({error_type}): {error_msg}"
                            )
                    else:
                        error_details.append(f"Node {node_id}: {node_error}")

            # Check if the error data itself contains validation info
            if error_data.get("type") == "prompt_outputs_failed_validation":
                error_message = error_data.get("message", "Workflow validation failed")
                # For this type of error, we need to parse the validation details from logs
                # Since ComfyUI doesn't seem to include detailed validation errors in the response
                # Let's provide a more helpful generic message
                available_models = get_available_models()
                if available_models.get("checkpoints"):
                    error_message += f"\n\nThis usually means a required model or parameter is not available."
                    error_message += f"\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                else:
                    error_message += "\n\nThis usually means a required model or parameter is not available."
                    error_message += "\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(error_message)

            # If we have specific validation errors, format them nicely
            if error_details:
                detailed_message = f"{error_message}:\n" + "\n".join(
                    f"• {detail}" for detail in error_details
                )

                # Try to provide helpful suggestions for common errors
                if any(
                    "not in list" in detail and "ckpt_name" in detail
                    for detail in error_details
                ):
                    available_models = get_available_models()
                    if available_models.get("checkpoints"):
                        detailed_message += f"\n\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                    else:
                        detailed_message += "\n\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(detailed_message)
            else:
                # Fallback to the raw response if we can't parse specific errors
                raise ValueError(f"{error_message}. Raw response: {response.text}")

        except (json.JSONDecodeError, KeyError) as e:
            # If we can't parse the error response, fall back to the raw text
            raise ValueError(
                f"ComfyUI validation failed (could not parse error response): {response.text}"
            )

    # For other HTTP errors, raise them normally
    response.raise_for_status()
    return response.json()


def get_history(prompt_id):
    """
    Retrieve the history of a given prompt using its ID

    Args:
        prompt_id (str): The ID of the prompt whose history is to be retrieved

    Returns:
        dict: The history of the prompt, containing all the processing steps and results
    """
    # Use requests for consistency and timeout
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def get_image_data(filename, subfolder, image_type):
    """
    Fetch image bytes from the ComfyUI /view endpoint.

    Args:
        filename (str): The filename of the image.
        subfolder (str): The subfolder where the image is stored.
        image_type (str): The type of the image (e.g., 'output').

    Returns:
        bytes: The raw image data, or None if an error occurs.
    """
    print(
        f"worker-comfyui - Fetching image data: type={image_type}, subfolder={subfolder}, filename={filename}"
    )
    data = {"filename": filename, "subfolder": subfolder, "type": image_type}
    url_values = urllib.parse.urlencode(data)
    try:
        # Use requests for consistency and timeout
        response = requests.get(f"http://{COMFY_HOST}/view?{url_values}", timeout=60)
        response.raise_for_status()
        print(f"worker-comfyui - Successfully fetched image data for {filename}")
        return response.content
    except requests.Timeout:
        print(f"worker-comfyui - Timeout fetching image data for {filename}")
        return None
    except requests.RequestException as e:
        print(f"worker-comfyui - Error fetching image data for {filename}: {e}")
        return None
    except Exception as e:
        print(
            f"worker-comfyui - Unexpected error fetching image data for {filename}: {e}"
        )
        return None


def handler(job):
    """
    Handles a job using ComfyUI via websockets for status and image retrieval.

    Args:
        job (dict): A dictionary containing job details and input parameters.

    Returns:
        dict: A dictionary containing either an error message or a success status with generated images.
    """
    job_input = job["input"]["fill.json"]
    job_id = job["id"]

    # Make sure that the input is valid
    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    # Extract validated data
    workflow = validated_data["workflow"]
    input_images = validated_data.get("images")

    # Make sure that the ComfyUI HTTP API is available before proceeding
    if not check_server(
        f"http://{COMFY_HOST}/",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return {
            "error": f"ComfyUI server ({COMFY_HOST}) not reachable after multiple retries."
        }

    # Upload input images if they exist
    if input_images:
        upload_result = upload_images(input_images)
        if upload_result["status"] == "error":
            # Return upload errors
            return {
                "error": "Failed to upload one or more input images",
                "details": upload_result["details"],
            }
        
    async def run_inference(job):
    job_input = job["input"]

    # --- Load Workflow JSON ---
    workflow_name = job_input.get("workflow_name", "fill").lower() # Default to fill
    if workflow_name == "fill":
        # Load your fill.json content here (you can read it from a file or embed it as a string)
        # For simplicity, let's assume you've read it into a variable called fill_workflow_json
        # In a real scenario, you'd load from a file:
        # with open("fill.json", "r") as f:
        #    workflow_json = json.load(f)
        # For now, placeholder:
        workflow_json = {"id":"00000000-0000-0000-0000-000000000000","revision":0,"last_node_id":57,"last_link_id":80,"nodes":[{"id":1,"type":"DualCLIPLoader","pos":[221,1203.5704345703125],"size":[337.76861572265625,130],"flags":{},"order":0,"mode":0,"inputs":[],"outputs":[{"name":"CLIP","type":"CLIP","links":[59]}],"properties":{"Node name for S&R":"DualCLIPLoader","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.38","models":[{"name":"clip_l.safetensors","url":"https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors","directory":"text_encoders","directory_invalid":true},{"name":"t5xxl_fp8_e4m3fn_scaled.safetensors","url":"https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors","directory":"text_encoders","directory_invalid":true}]},"widgets_values":["clip_l.safetensors","t5xxl_fp8_e4m3fn_scaled.safetensors","flux","default"],"color":"#232","bgcolor":"#353","serialize_values":[null,null,null,null]},{"id":2,"type":"Reroute","pos":[648.411376953125,888.510986328125],"size":[75,26],"flags":{},"order":5,"mode":0,"inputs":[{"name":"","type":"*","link":1}],"outputs":[{"name":"","type":"MODEL","links":[2]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}},"color":"#232","bgcolor":"#353"},{"id":3,"type":"Reroute","pos":[2329.1015625,890.4904174804688],"size":[75,26],"flags":{},"order":10,"mode":0,"inputs":[{"name":"","type":"*","link":2}],"outputs":[{"name":"","type":"MODEL","links":[6]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}},"color":"#232","bgcolor":"#353"},{"id":4,"type":"VAEEncode","pos":[1504.572021484375,1359.4615478515625],"size":[140,46],"flags":{"collapsed":false},"order":12,"mode":0,"inputs":[{"name":"pixels","type":"IMAGE","link":3},{"name":"vae","type":"VAE","link":4}],"outputs":[{"name":"LATENT","type":"LATENT","links":[10,17]}],"properties":{"Node name for S&R":"VAEEncode","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.39"},"widgets_values":[],"color":"#232","bgcolor":"#353","serialize_values":[]},{"id":5,"type":"Reroute","pos":[2665.81884765625,2409.03564453125],"size":[75,26],"flags":{},"order":11,"mode":0,"inputs":[{"name":"","type":"*","link":5}],"outputs":[{"name":"","type":"VAE","links":[53]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}},"color":"#232","bgcolor":"#353"},{"id":7,"type":"Reroute","pos":[2215.71630859375,1358.265625],"size":[75,26],"flags":{},"order":15,"mode":0,"inputs":[{"name":"","type":"*","link":10}],"outputs":[{"name":"","type":"LATENT","links":[9]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}},"color":"#232","bgcolor":"#353"},{"id":8,"type":"PreviewImage","pos":[1514.0106201171875,1791.860595703125],"size":[420,310],"flags":{},"order":13,"mode":0,"inputs":[{"name":"images","type":"IMAGE","link":11}],"outputs":[],"properties":{"Node name for S&R":"PreviewImage","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.40"},"widgets_values":[],"color":"#232","bgcolor":"#353","serialize_values":[]},{"id":10,"type":"Reroute","pos":[1532.2562255859375,1144.4141845703125],"size":[75,26],"flags":{},"order":8,"mode":0,"inputs":[{"name":"","type":"*","link":13}],"outputs":[{"name":"","type":"CONDITIONING","links":[16]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}},"color":"#232","bgcolor":"#353"},{"id":11,"type":"ConditioningZeroOut","pos":[1637.8150634765625,1067.813720703125],"size":[240,26],"flags":{"collapsed":false},"order":9,"mode":0,"inputs":[{"name":"conditioning","type":"CONDITIONING","link":14}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","links":[8]}],"properties":{"Node name for S&R":"ConditioningZeroOut","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.39"},"widgets_values":[],"color":"#232","bgcolor":"#353","serialize_values":[]},{"id":12,"type":"Reroute","pos":[805.606689453125,2406.8447265625],"size":[75,26],"flags":{},"order":6,"mode":0,"inputs":[{"name":"","type":"*","link":15}],"outputs":[{"name":"","type":"VAE","links":[5]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}},"color":"#232","bgcolor":"#353"},{"id":13,"type":"ReferenceLatent","pos":[1912.32763671875,1141.27197265625],"size":[197.712890625,46],"flags":{},"order":16,"mode":0,"inputs":[{"name":"conditioning","type":"CONDITIONING","link":16},{"name":"latent","shape":7,"type":"LATENT","link":17}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","links":[18]}],"properties":{"Node name for S&R":"ReferenceLatent","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.41"},"widgets_values":[],"color":"#232","bgcolor":"#353","serialize_values":[]},{"id":39,"type":"FluxKontextImageScale","pos":[1175.8065185546875,1558.818115234375],"size":[270,30],"flags":{"collapsed":false},"order":7,"mode":0,"inputs":[{"name":"image","type":"IMAGE","link":67}],"outputs":[{"name":"IMAGE","type":"IMAGE","links":[3,11,54]}],"properties":{"Node name for S&R":"FluxKontextImageScale","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.38"},"widgets_values":[],"color":"#232","bgcolor":"#353","serialize_values":[]},{"id":42,"type":"SaveImage","pos":[3293.671142578125,1363.122802734375],"size":[535.7857055664062,510.8869323730469],"flags":{},"order":20,"mode":0,"inputs":[{"name":"images","type":"IMAGE","link":49}],"outputs":[],"properties":{"Node name for S&R":"SaveImage","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.39"},"widgets_values":["ComfyUI"],"color":"#232","bgcolor":"#353","serialize_values":[null]},{"id":45,"type":"VAEDecode","pos":[2987.949951171875,1274.70654296875],"size":[190,46],"flags":{"collapsed":false},"order":19,"mode":0,"inputs":[{"name":"samples","type":"LATENT","link":52},{"name":"vae","type":"VAE","link":53}],"outputs":[{"name":"IMAGE","type":"IMAGE","slot_index":0,"links":[49]}],"properties":{"Node name for S&R":"VAEDecode","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.38"},"widgets_values":[],"color":"#232","bgcolor":"#353","serialize_values":[]},{"id":46,"type":"UNETLoader","pos":[221,1073.5704345703125],"size":[337.76861572265625,82],"flags":{},"order":1,"mode":0,"inputs":[],"outputs":[{"name":"MODEL","type":"MODEL","links":[1]}],"properties":{"Node name for S&R":"UNETLoader","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.38","models":[{"name":"flux1-dev-kontext_fp8_scaled.safetensors","url":"https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors","directory":"diffusion_models","directory_invalid":true}]},"widgets_values":["FLUX.1-Kontext-dev-1","default"],"color":"#232","bgcolor":"#353","serialize_values":["d1f2p2de878c73f2ok1g@e18ce6bc07d1c1c479d5b8102dbd319d",null]},{"id":47,"type":"VAELoader","pos":[221,1383.5704345703125],"size":[337.76861572265625,58],"flags":{},"order":2,"mode":0,"inputs":[],"outputs":[{"name":"VAE","type":"VAE","links":[4,15]}],"properties":{"Node name for S&R":"VAELoader","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.38","models":[{"name":"ae.safetensors","url":"https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors","directory":"vae","directory_invalid":true}]},"widgets_values":["ae.sft"],"color":"#232","bgcolor":"#353","serialize_values":[null]},{"id":48,"type":"Reroute","pos":[1986.1007080078125,1565.721435546875],"size":[75,26],"flags":{},"order":14,"mode":0,"inputs":[{"name":"","type":"*","link":54}],"outputs":[{"name":"","type":"IMAGE","links":[]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}},"color":"#232","bgcolor":"#353"},{"id":43,"type":"CLIPTextEncode","pos":[1090.8553466796875,1067.408447265625],"size":[400,220],"flags":{},"order":4,"mode":0,"inputs":[{"name":"clip","type":"CLIP","link":59}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","slot_index":0,"links":[13,14]}],"title":"CLIP Text Encode (Positive Prompt)","properties":{"Node name for S&R":"CLIPTextEncode","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.38"},"widgets_values":["Fill this empty room with {a} in {b} with {c} style. Arrange the furniture naturally to make the space feel inviting and functional. Use realistic proportions and details. Include warm ambient lighting from ceiling fixtures or lamps to enhance the cozy atmosphere, with soft shadows and natural light from windows if visible. Focus on a photorealistic interior design look.\n",[false,true]],"color":"#232","bgcolor":"#353","serialize_values":[null,null]},{"id":14,"type":"FluxGuidance","pos":[2168.962158203125,986.6275024414062],"size":[240,58],"flags":{"collapsed":false},"order":17,"mode":0,"inputs":[{"name":"conditioning","type":"CONDITIONING","link":18}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","slot_index":0,"links":[7]}],"properties":{"Node name for S&R":"FluxGuidance","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.38"},"widgets_values":[2.5],"color":"#232","bgcolor":"#353","serialize_values":[null]},{"id":6,"type":"KSampler","pos":[2520.31640625,1029.4554443359375],"size":[320,262],"flags":{},"order":18,"mode":0,"inputs":[{"name":"model","type":"MODEL","link":6},{"name":"positive","type":"CONDITIONING","link":7},{"name":"negative","type":"CONDITIONING","link":8},{"name":"latent_image","type":"LATENT","link":9}],"outputs":[{"name":"LATENT","type":"LATENT","slot_index":0,"links":[52]}],"properties":{"Node name for S&R":"KSampler","widget_ue_connectable":{},"cnr_id":"comfy-core","ver":"0.3.38"},"widgets_values":[350530340099539,"randomize",20,1,"euler","normal",1],"color":"#232","bgcolor":"#353","serialize_values":[null,null,null,null,null,null,null]},{"id":57,"type":"LoadImageFromUrl","pos":[717.0623168945312,1559.1307373046875],"size":[400,230],"flags":{},"order":3,"mode":0,"inputs":[],"outputs":[{"name":"images","shape":6,"type":"IMAGE","links":[67]},{"name":"masks","shape":6,"type":"MASK","links":null},{"name":"has_image","type":"BOOLEAN","links":null}],"properties":{"Node name for S&R":"LoadImageFromUrl"},"widgets_values":{"image":"","keep_alpha_channel":false,"output_mode":false,"choose image to upload":"image"},"serialize_values":[null,null,null,null]}],"links":[[1,46,0,2,0,"*"],[2,2,0,3,0,"*"],[3,39,0,4,0,"IMAGE"],[4,47,0,4,1,"VAE"],[5,12,0,5,0,"*"],[6,3,0,6,0,"MODEL"],[7,14,0,6,1,"CONDITIONING"],[8,11,0,6,2,"CONDITIONING"],[9,7,0,6,3,"LATENT"],[10,4,0,7,0,"*"],[11,39,0,8,0,"IMAGE"],[13,43,0,10,0,"*"],[14,43,0,11,0,"CONDITIONING"],[15,47,0,12,0,"*"],[16,10,0,13,0,"CONDITIONING"],[17,4,0,13,1,"LATENT"],[18,13,0,14,0,"CONDITIONING"],[49,45,0,42,0,"IMAGE"],[52,6,0,45,0,"LATENT"],[53,5,0,45,1,"VAE"],[54,39,0,48,0,"*"],[59,1,0,43,0,"CLIP"],[67,57,0,39,0,"IMAGE"]],"groups":[{"id":2,"title":"Group","bounding":[211,844.9110107421875,3628.456787109375,1600.1246337890625],"color":"#A88","font_size":24,"flags":{}}],"config":{},"extra":{"ue_links":[],"ds":{"scale":0.31384283767210325,"offset":[-929.3364359271673,-284.11178411681635]},"frontendVersion":"1.19.9","links_added_by_ue":[]},"version":0.4} # Paste fill.json content here
    elif workflow_name == "redesign":
        # Load your Redesign.json content here
        workflow_json = {"id":"00000000-0000-0000-0000-000000000000","revision":0,"last_node_id":71,"last_link_id":99,"nodes":[{"id":1,"type":"DualCLIPLoader","pos":[304,593.1249389648438],"size":[337.76861572265625,130],"flags":{},"order":0,"mode":0,"inputs":[],"outputs":[{"name":"CLIP","type":"CLIP","links":[18]}],"properties":{"Node name for S&R":"DualCLIPLoader","cnr_id":"comfy-core","ver":"0.3.38","models":[{"name":"clip_l.safetensors","url":"https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors","directory":"text_encoders","directory_invalid":true},{"name":"t5xxl_fp8_e4m3fn_scaled.safetensors","url":"https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors","directory":"text_encoders","directory_invalid":true}],"widget_ue_connectable":{}},"widgets_values":["clip_l.safetensors","t5xxl_fp8_e4m3fn_scaled.safetensors","flux","default"],"color":"#322","bgcolor":"#533","serialize_values":[null,null,null,null]},{"id":2,"type":"VAEEncode","pos":[1587.572021484375,749.0160522460938],"size":[140,46],"flags":{"collapsed":false},"order":17,"mode":0,"inputs":[{"name":"pixels","type":"IMAGE","link":1},{"name":"vae","type":"VAE","link":2}],"outputs":[{"name":"LATENT","type":"LATENT","links":[7,13]}],"properties":{"Node name for S&R":"VAEEncode","cnr_id":"comfy-core","ver":"0.3.39","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[]},{"id":3,"type":"KSampler","pos":[2603.31640625,419.010009765625],"size":[320,262],"flags":{},"order":36,"mode":0,"inputs":[{"name":"model","type":"MODEL","link":3},{"name":"positive","type":"CONDITIONING","link":4},{"name":"negative","type":"CONDITIONING","link":5},{"name":"latent_image","type":"LATENT","link":6}],"outputs":[{"name":"LATENT","type":"LATENT","slot_index":0,"links":[25]}],"properties":{"Node name for S&R":"KSampler","cnr_id":"comfy-core","ver":"0.3.38","widget_ue_connectable":{}},"widgets_values":[601619060235283,"randomize",20,1,"euler","simple",1],"serialize_values":[null,null,null,null,null,null,null]},{"id":4,"type":"Reroute","pos":[2298.71630859375,747.8201293945312],"size":[75,26],"flags":{},"order":25,"mode":0,"inputs":[{"name":"","type":"*","link":7}],"outputs":[{"name":"","type":"LATENT","links":[6]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":5,"type":"PreviewImage","pos":[1597.0106201171875,1181.415283203125],"size":[420,310],"flags":{},"order":18,"mode":0,"inputs":[{"name":"images","type":"IMAGE","link":8}],"outputs":[],"properties":{"Node name for S&R":"PreviewImage","cnr_id":"comfy-core","ver":"0.3.40","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[null]},{"id":6,"type":"Reroute","pos":[1615.2562255859375,533.9686889648438],"size":[75,26],"flags":{},"order":21,"mode":0,"inputs":[{"name":"","type":"*","link":9}],"outputs":[{"name":"","type":"CONDITIONING","links":[12]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":7,"type":"ConditioningZeroOut","pos":[1720.8150634765625,457.36822509765625],"size":[240,26],"flags":{"collapsed":false},"order":22,"mode":0,"inputs":[{"name":"conditioning","type":"CONDITIONING","link":10}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","links":[5]}],"properties":{"Node name for S&R":"ConditioningZeroOut","cnr_id":"comfy-core","ver":"0.3.39","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[]},{"id":8,"type":"Reroute","pos":[888.606689453125,1796.3994140625],"size":[75,26],"flags":{},"order":9,"mode":0,"inputs":[{"name":"","type":"*","link":11}],"outputs":[{"name":"","type":"VAE","links":[22]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":9,"type":"ReferenceLatent","pos":[1995.32763671875,530.826416015625],"size":[197.712890625,46],"flags":{},"order":27,"mode":0,"inputs":[{"name":"conditioning","type":"CONDITIONING","link":12},{"name":"latent","shape":7,"type":"LATENT","link":13}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","links":[14]}],"properties":{"Node name for S&R":"ReferenceLatent","cnr_id":"comfy-core","ver":"0.3.41","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[]},{"id":10,"type":"FluxGuidance","pos":[2251.962158203125,376.1820373535156],"size":[240,58],"flags":{"collapsed":false},"order":32,"mode":0,"inputs":[{"name":"conditioning","type":"CONDITIONING","link":14}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","slot_index":0,"links":[4]}],"properties":{"Node name for S&R":"FluxGuidance","cnr_id":"comfy-core","ver":"0.3.38","widget_ue_connectable":{}},"widgets_values":[2.5],"serialize_values":[null]},{"id":11,"type":"FluxKontextImageScale","pos":[1258.8065185546875,948.3726196289062],"size":[270,30],"flags":{"collapsed":false},"order":10,"mode":0,"inputs":[{"name":"image","type":"IMAGE","link":81}],"outputs":[{"name":"IMAGE","type":"IMAGE","links":[1,8]}],"properties":{"Node name for S&R":"FluxKontextImageScale","cnr_id":"comfy-core","ver":"0.3.38","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[]},{"id":12,"type":"UNETLoader","pos":[304,463.12493896484375],"size":[337.76861572265625,82],"flags":{},"order":1,"mode":0,"inputs":[],"outputs":[{"name":"MODEL","type":"MODEL","links":[20]}],"properties":{"Node name for S&R":"UNETLoader","cnr_id":"comfy-core","ver":"0.3.38","models":[{"name":"flux1-dev-kontext_fp8_scaled.safetensors","url":"https://huggingface.co/Comfy-Org/flux1-kontext-dev_ComfyUI/resolve/main/split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors","directory":"diffusion_models","directory_invalid":true}],"widget_ue_connectable":{}},"widgets_values":["FLUX.1-Kontext-dev-1","default"],"color":"#322","bgcolor":"#533","serialize_values":["d1f2p2de878c73f2ok1g@e18ce6bc07d1c1c479d5b8102dbd319d",null]},{"id":13,"type":"VAELoader","pos":[304,773.1249389648438],"size":[337.76861572265625,58],"flags":{},"order":2,"mode":0,"inputs":[],"outputs":[{"name":"VAE","type":"VAE","links":[2,11]}],"properties":{"Node name for S&R":"VAELoader","cnr_id":"comfy-core","ver":"0.3.38","models":[{"name":"ae.safetensors","url":"https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors","directory":"vae","directory_invalid":true}],"widget_ue_connectable":{}},"widgets_values":["ae.sft"],"color":"#322","bgcolor":"#533","serialize_values":[null]},{"id":15,"type":"CLIPTextEncode","pos":[1173.8553466796875,456.96295166015625],"size":[400,220],"flags":{},"order":13,"mode":0,"inputs":[{"name":"clip","type":"CLIP","link":16}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","slot_index":0,"links":[9,10]}],"title":"CLIP Text Encode (Positive Prompt)","properties":{"Node name for S&R":"CLIPTextEncode","cnr_id":"comfy-core","ver":"0.3.38","widget_ue_connectable":{}},"widgets_values":["remove all the furniture like sofas, tables, plants, lights, fireplace, paintings, curtains and carpet",[false,true]],"color":"#232","bgcolor":"#353","serialize_values":[null,null]},{"id":16,"type":"Reroute","pos":[2412.1015625,280.044921875],"size":[75,26],"flags":{},"order":15,"mode":0,"inputs":[{"name":"","type":"*","link":17}],"outputs":[{"name":"","type":"MODEL","links":[3]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":19,"type":"Reroute","pos":[731.411376953125,278.0655212402344],"size":[75,26],"flags":{},"order":8,"mode":0,"inputs":[{"name":"","type":"*","link":20}],"outputs":[{"name":"","type":"MODEL","links":[17]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":23,"type":"SaveImage","pos":[3522.84716796875,345.2018737792969],"size":[473.6256408691406,421.7007751464844],"flags":{},"order":44,"mode":0,"inputs":[{"name":"images","type":"IMAGE","link":24}],"outputs":[],"properties":{"Node name for S&R":"SaveImage","cnr_id":"comfy-core","ver":"0.3.39","widget_ue_connectable":{}},"widgets_values":["ComfyUI"],"serialize_values":[null,null]},{"id":30,"type":"UNETLoader","pos":[6911.91552734375,556.8027954101562],"size":[315,82],"flags":{},"order":3,"mode":0,"inputs":[],"outputs":[{"name":"MODEL","type":"MODEL","slot_index":0,"links":[28]}],"properties":{"Node name for S&R":"UNETLoader","widget_ue_connectable":{}},"widgets_values":["FLUX-Dev","fp8_e4m3fn"],"serialize_values":["5c4f26337a770760d2d6d2288d7e4698@86f5b35dcf257ab74b9fa00a8bbf0da3",null]},{"id":41,"type":"CLIPTextEncodeFlux","pos":[6151.25439453125,1044.401123046875],"size":[398.8999938964844,166],"flags":{},"order":43,"mode":0,"inputs":[{"name":"clip","type":"CLIP","link":60}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","slot_index":0,"links":[68]}],"properties":{"Node name for S&R":"CLIPTextEncodeFlux","widget_ue_connectable":{}},"widgets_values":["bad photo","bad photo",4,[false,true],[false,true]],"serialize_values":[null,null,null,null,null]},{"id":40,"type":"CLIPTextEncodeFlux","pos":[6252.9619140625,819.0371704101562],"size":[286.951416015625,166],"flags":{},"order":37,"mode":0,"inputs":[{"name":"clip","type":"CLIP","link":58},{"name":"clip_l","type":"STRING","widget":{"name":"clip_l"},"link":62},{"name":"t5xxl","type":"STRING","widget":{"name":"t5xxl"},"link":61}],"outputs":[{"name":"CONDITIONING","type":"CONDITIONING","slot_index":0,"links":[69]}],"properties":{"Node name for S&R":"CLIPTextEncodeFlux","widget_ue_connectable":{"clip_l":true,"t5xxl":true}},"widgets_values":["A mysterious female android standing in a dimly lit room, partially silhouetted against soft backlighting. She has a sleek, black, metallic body with visible mechanical joints, and is wearing a silky, lace-trimmed nightgown with a loose robe slipping off her shoulders. The ambient lighting is low and cinematic, casting dramatic shadows and emphasizing the reflective texture of her synthetic skin. The atmosphere is moody and intimate, with a quiet, noir-inspired tone. Her short hair and elegant pose evoke both beauty and isolation. Style of analog film photography, with soft grain and subtle color gradients.\n","A mysterious female android standing in a dimly lit room, partially silhouetted against soft backlighting. She has a sleek, black, metallic body with visible mechanical joints, and is wearing a silky, lace-trimmed nightgown with a loose robe slipping off her shoulders. The ambient lighting is low and cinematic, casting dramatic shadows and emphasizing the reflective texture of her synthetic skin. The atmosphere is moody and intimate, with a quiet, noir-inspired tone. Her short hair and elegant pose evoke both beauty and isolation. Style of analog film photography, with soft grain and subtle color gradients.\n",4,[false,true],[false,true]],"serialize_values":[null,null,null,null,null]},{"id":47,"type":"Reroute","pos":[6047.88037109375,842.3867797851562],"size":[75,26],"flags":{},"order":20,"mode":0,"inputs":[{"name":"","type":"*","widget":{"name":"value"},"link":54}],"outputs":[{"name":"","type":"STRING","links":[62]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":50,"type":"Reroute","pos":[5307.75830078125,557.0555419921875],"size":[75,26],"flags":{},"order":31,"mode":0,"inputs":[{"name":"","type":"*","link":64}],"outputs":[{"name":"","type":"IMAGE","links":[65]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":31,"type":"LoadFluxControlNet","pos":[4888.4150390625,538.117919921875],"size":[316.83343505859375,86.47058868408203],"flags":{},"order":4,"mode":0,"inputs":[],"outputs":[{"name":"ControlNet","type":"FluxControlNet","slot_index":0,"links":[33]}],"properties":{"Node name for S&R":"LoadFluxControlNet","widget_ue_connectable":{}},"widgets_values":["flux-dev-fp8","flux-depth-controlnet-v3.safetensors"],"serialize_values":[null,null]},{"id":38,"type":"PreviewImage","pos":[5412.35546875,629.8264770507812],"size":[245.43121337890625,246],"flags":{"collapsed":false},"order":30,"mode":0,"inputs":[{"name":"images","type":"IMAGE","link":41}],"outputs":[],"properties":{"Node name for S&R":"PreviewImage","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[null]},{"id":46,"type":"Reroute","pos":[5938.3876953125,895.7615966796875],"size":[75,26],"flags":{},"order":12,"mode":0,"inputs":[{"name":"","type":"*","widget":{"name":"value"},"link":85}],"outputs":[{"name":"","type":"STRING","links":[54,61]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":52,"type":"Reroute","pos":[6656.38427734375,845.3936767578125],"size":[75,26],"flags":{},"order":46,"mode":0,"inputs":[{"name":"","type":"*","link":68}],"outputs":[{"name":"","type":"CONDITIONING","links":[71]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":53,"type":"Reroute","pos":[6567.28125,822.201171875],"size":[75,26],"flags":{},"order":42,"mode":0,"inputs":[{"name":"","type":"*","link":69}],"outputs":[{"name":"","type":"CONDITIONING","links":[70]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":43,"type":"VAEDecode","pos":[7856.884765625,857.2550659179688],"size":[210,46],"flags":{},"order":53,"mode":0,"inputs":[{"name":"samples","type":"LATENT","link":48},{"name":"vae","type":"VAE","link":80}],"outputs":[{"name":"IMAGE","type":"IMAGE","slot_index":0,"links":[72]}],"properties":{"Node name for S&R":"VAEDecode","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[]},{"id":48,"type":"Reroute","pos":[5172.43603515625,827.53857421875],"size":[75,26],"flags":{},"order":33,"mode":0,"inputs":[{"name":"","type":"*","link":76}],"outputs":[{"name":"","type":"CLIP","links":[58,59]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":32,"type":"DepthAnythingV2Preprocessor","pos":[4808.859375,687.150146484375],"size":[340.20001220703125,82],"flags":{},"order":26,"mode":0,"inputs":[{"name":"image","type":"IMAGE","link":84}],"outputs":[{"name":"IMAGE","type":"IMAGE","slot_index":0,"links":[41,64]}],"properties":{"Node name for S&R":"DepthAnythingV2Preprocessor","widget_ue_connectable":{}},"widgets_values":["depth_anything_v2_vitl.pth",1024],"serialize_values":[null,null]},{"id":17,"type":"Reroute","pos":[749.232666015625,453.607421875],"size":[75,26],"flags":{},"order":7,"mode":0,"inputs":[{"name":"","type":"*","link":18}],"outputs":[{"name":"","type":"CLIP","links":[16,73]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":55,"type":"Reroute","pos":[974.3146362304688,179.23097229003906],"size":[75,26],"flags":{},"order":14,"mode":0,"inputs":[{"name":"","type":"*","link":73}],"outputs":[{"name":"","type":"CLIP","links":[74]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":56,"type":"Reroute","pos":[4162.10205078125,184.26304626464844],"size":[75,26],"flags":{},"order":23,"mode":0,"inputs":[{"name":"","type":"*","link":74}],"outputs":[{"name":"","type":"CLIP","links":[75]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":21,"type":"Reroute","pos":[2748.81884765625,1798.590087890625],"size":[75,26],"flags":{},"order":16,"mode":0,"inputs":[{"name":"","type":"*","link":22}],"outputs":[{"name":"","type":"VAE","links":[26,77]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":58,"type":"Reroute","pos":[2903.285400390625,1932.16259765625],"size":[75,26],"flags":{},"order":24,"mode":0,"inputs":[{"name":"","type":"*","link":77}],"outputs":[{"name":"","type":"VAE","links":[78]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":60,"type":"Reroute","pos":[7633.37841796875,1469.798828125],"size":[75,26],"flags":{},"order":39,"mode":0,"inputs":[{"name":"","type":"*","link":90}],"outputs":[{"name":"","type":"VAE","links":[80]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":14,"type":"LoadImage","pos":[868.5282592773438,950.1412963867188],"size":[274.080078125,314],"flags":{},"order":5,"mode":0,"inputs":[],"outputs":[{"name":"IMAGE","type":"IMAGE","links":[81,82]},{"name":"MASK","type":"MASK","links":null}],"properties":{"Node name for S&R":"LoadImage","cnr_id":"comfy-core","ver":"0.3.43","widget_ue_connectable":{}},"widgets_values":["https://image.cdn2.seaart.me/20250702/0809cf62-c342-41b6-be45-3f87b157d02b.jpg","768@768"],"serialize_values":[null,null,null]},{"id":61,"type":"Reroute","pos":[3876.81396484375,947.2115478515625],"size":[75,26],"flags":{},"order":11,"mode":4,"inputs":[{"name":"","type":"*","link":82}],"outputs":[{"name":"","type":"IMAGE","links":[83]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":62,"type":"Reroute","pos":[4132.47265625,690.6267700195312],"size":[75,26],"flags":{},"order":19,"mode":0,"inputs":[{"name":"","type":"*","link":83}],"outputs":[{"name":"","type":"IMAGE","links":[84]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":57,"type":"Reroute","pos":[4522.814453125,829.514892578125],"size":[75,26],"flags":{},"order":28,"mode":0,"inputs":[{"name":"","type":"*","link":75}],"outputs":[{"name":"","type":"CLIP","links":[76]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":24,"type":"VAEDecode","pos":[3070.949951171875,664.2610473632812],"size":[190,46],"flags":{"collapsed":false},"order":41,"mode":0,"inputs":[{"name":"samples","type":"LATENT","link":25},{"name":"vae","type":"VAE","link":26}],"outputs":[{"name":"IMAGE","type":"IMAGE","slot_index":0,"links":[24,86]}],"properties":{"Node name for S&R":"VAEDecode","cnr_id":"comfy-core","ver":"0.3.38","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[]},{"id":67,"type":"Reroute","pos":[7413.12890625,1911.987060546875],"size":[75,26],"flags":{},"order":34,"mode":0,"inputs":[{"name":"","type":"*","link":89}],"outputs":[{"name":"","type":"VAE","links":[90]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":59,"type":"Reroute","pos":[6520.79248046875,1908.0357666015625],"size":[75,26],"flags":{},"order":29,"mode":0,"inputs":[{"name":"","type":"*","link":78}],"outputs":[{"name":"","type":"VAE","links":[89,91]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":66,"type":"VAEEncode","pos":[6799.41357421875,1293.9183349609375],"size":[210,46],"flags":{},"order":49,"mode":0,"inputs":[{"name":"pixels","type":"IMAGE","link":88},{"name":"vae","type":"VAE","link":91}],"outputs":[{"name":"LATENT","type":"LATENT","links":[]}],"properties":{"Node name for S&R":"VAEEncode","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[]},{"id":54,"type":"PreviewImage","pos":[8171.24853515625,856.6873168945312],"size":[210,246],"flags":{},"order":54,"mode":0,"inputs":[{"name":"images","type":"IMAGE","link":72}],"outputs":[],"properties":{"Node name for S&R":"PreviewImage","widget_ue_connectable":{}},"widgets_values":[],"serialize_values":[null]},{"id":49,"type":"Reroute","pos":[5365.4541015625,1045.2410888671875],"size":[75,26],"flags":{},"order":38,"mode":0,"inputs":[{"name":"","type":"*","link":59}],"outputs":[{"name":"","type":"CLIP","links":[60]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":51,"type":"Reroute","pos":[6805.29150390625,884.6040649414062],"size":[75,26],"flags":{},"order":40,"mode":0,"inputs":[{"name":"","type":"*","link":66}],"outputs":[{"name":"","type":"ControlNetCondition","links":[67]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":69,"type":"Resize Image for SDXL","pos":[6111.013671875,1296.4296875],"size":[353.9320068359375,82],"flags":{},"order":47,"mode":0,"inputs":[{"name":"image","type":"IMAGE","link":95}],"outputs":[{"name":"IMAGE","type":"IMAGE","links":[94]}],"properties":{"Node name for S&R":"Resize Image for SDXL","widget_ue_connectable":{}},"widgets_values":["nearest-exact","disabled"],"serialize_values":[null,null]},{"id":64,"type":"Reroute","pos":[3492.7978515625,1312.4742431640625],"size":[75,26],"flags":{},"order":45,"mode":4,"inputs":[{"name":"","type":"*","link":86}],"outputs":[{"name":"","type":"IMAGE","links":[95]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":27,"type":"XlabsSampler","pos":[7393.91748046875,802.0604858398438],"size":[342.5999755859375,282],"flags":{},"order":52,"mode":0,"inputs":[{"name":"model","type":"MODEL","link":28},{"name":"conditioning","type":"CONDITIONING","link":70},{"name":"neg_conditioning","type":"CONDITIONING","link":71},{"name":"latent_image","shape":7,"type":"LATENT","link":99},{"name":"controlnet_condition","shape":7,"type":"ControlNetCondition","link":67}],"outputs":[{"name":"latent","type":"LATENT","links":[48]}],"properties":{"Node name for S&R":"XlabsSampler","widget_ue_connectable":{}},"widgets_values":[839339050288597,"randomize",25,1,3.5,0,1],"serialize_values":[null,null,null,null,null,null,null]},{"id":65,"type":"Reroute","pos":[6510.3984375,1295.1156005859375],"size":[75,26],"flags":{},"order":48,"mode":0,"inputs":[{"name":"","type":"*","link":94}],"outputs":[{"name":"","type":"IMAGE","links":[88,96]}],"properties":{"showOutputText":false,"horizontal":false,"widget_ue_connectable":{}}},{"id":70,"type":"GetImageSize+","pos":[6762.7763671875,1150.786865234375],"size":[239.25233459472656,66],"flags":{},"order":50,"mode":0,"inputs":[{"name":"image","type":"IMAGE","link":96}],"outputs":[{"name":"width","type":"INT","links":[97]},{"name":"height","type":"INT","links":[98]},{"name":"count","type":"INT","links":null}],"properties":{"Node name for S&R":"GetImageSize+"}},{"id":71,"type":"EmptyLatentImage","pos":[7021.36083984375,1126.4989013671875],"size":[255.90908813476562,106],"flags":{},"order":51,"mode":0,"inputs":[{"name":"width","type":"INT","widget":{"name":"width"},"link":97},{"name":"height","type":"INT","widget":{"name":"height"},"link":98}],"outputs":[{"name":"LATENT","type":"LATENT","links":[99]}],"properties":{"Node name for S&R":"EmptyLatentImage"},"widgets_values":[64,512,1],"serialize_values":[null,null,null]},{"id":28,"type":"ApplyFluxControlNet","pos":[6342.1484375,538.5428466796875],"size":[393,98],"flags":{},"order":35,"mode":0,"inputs":[{"name":"controlnet","type":"FluxControlNet","link":33},{"name":"image","type":"IMAGE","link":65},{"name":"controlnet_condition","shape":7,"type":"ControlNetCondition","link":null}],"outputs":[{"name":"controlnet_condition","type":"ControlNetCondition","slot_index":0,"links":[66]}],"properties":{"Node name for S&R":"ApplyFluxControlNet","widget_ue_connectable":{}},"widgets_values":[0.8000000000000002],"serialize_values":[null]},{"id":63,"type":"ttN text","pos":[5444.2900390625,897.278076171875],"size":[414.1118469238281,134.52102661132812],"flags":{},"order":6,"mode":0,"inputs":[],"outputs":[{"name":"text","type":"STRING","links":[85]}],"properties":{"Node name for S&R":"ttN text","widget_ue_connectable":{},"ttNnodeVersion":"1.0.0"},"widgets_values":["A modern, minimalist living room with a warm, natural color palette dominated by beige and light wood tones. The room features a large, plush beige sofa with oversized cushions, positioned next to wide windows offering a scenic view of green hills. Sheer curtains filter soft daylight. A low wooden coffee table with clean lines sits on a textured neutral rug, set for tea with white plates and cups. The space includes subtle green plants in vases adding freshness. A sleek, modern fireplace with a black metallic finish anchors the room, with minimalist decor items on the mantel. The ceiling is clad in light wood panels with a large circular ceiling light, casting a cozy ambient glow. The overall atmosphere is calm, bright, and elegant in a Japanese-Scandinavian inspired style.",[false,true]],"serialize_values":[null,null]}],"links":[[1,11,0,2,0,"IMAGE"],[2,13,0,2,1,"VAE"],[3,16,0,3,0,"MODEL"],[4,10,0,3,1,"CONDITIONING"],[5,7,0,3,2,"CONDITIONING"],[6,4,0,3,3,"LATENT"],[7,2,0,4,0,"*"],[8,11,0,5,0,"IMAGE"],[9,15,0,6,0,"*"],[10,15,0,7,0,"CONDITIONING"],[11,13,0,8,0,"*"],[12,6,0,9,0,"CONDITIONING"],[13,2,0,9,1,"LATENT"],[14,9,0,10,0,"CONDITIONING"],[16,17,0,15,0,"CLIP"],[17,19,0,16,0,"*"],[18,1,0,17,0,"*"],[20,12,0,19,0,"*"],[22,8,0,21,0,"*"],[24,24,0,23,0,"IMAGE"],[25,3,0,24,0,"LATENT"],[26,21,0,24,1,"VAE"],[28,30,0,27,0,"MODEL"],[33,31,0,28,0,"FluxControlNet"],[41,32,0,38,0,"IMAGE"],[48,27,0,43,0,"LATENT"],[54,46,0,47,0,"*"],[58,48,0,40,0,"CLIP"],[59,48,0,49,0,"*"],[60,49,0,41,0,"CLIP"],[61,46,0,40,2,"STRING"],[62,47,0,40,1,"STRING"],[64,32,0,50,0,"*"],[65,50,0,28,1,"IMAGE"],[66,28,0,51,0,"*"],[67,51,0,27,4,"ControlNetCondition"],[68,41,0,52,0,"*"],[69,40,0,53,0,"*"],[70,53,0,27,1,"CONDITIONING"],[71,52,0,27,2,"CONDITIONING"],[72,43,0,54,0,"IMAGE"],[73,17,0,55,0,"*"],[74,55,0,56,0,"*"],[75,56,0,57,0,"*"],[76,57,0,48,0,"*"],[77,21,0,58,0,"*"],[78,58,0,59,0,"*"],[80,60,0,43,1,"VAE"],[81,14,0,11,0,"IMAGE"],[82,14,0,61,0,"*"],[83,61,0,62,0,"*"],[84,62,0,32,0,"IMAGE"],[85,63,0,46,0,"*"],[86,24,0,64,0,"*"],[88,65,0,66,0,"IMAGE"],[89,59,0,67,0,"*"],[90,67,0,60,0,"*"],[91,59,0,66,1,"VAE"],[94,69,0,65,0,"*"],[95,64,0,69,0,"IMAGE"],[96,65,0,70,0,"IMAGE"],[97,70,0,71,0,"INT"],[98,70,1,71,1,"INT"],[99,71,0,27,3,"LATENT"]],"groups":[{"id":1,"title":"Remove Furniture","bounding":[294,234.46551513671875,3712.472900390625,1600.12451171875],"color":"#A88","font_size":24,"flags":{}},{"id":2,"title":"ControlNet","bounding":[4649.1298828125,395.47247314453125,3912.78662109375,1128.7896728515625],"color":"#3f789e","font_size":24,"flags":{}}],"config":{},"extra":{"ue_links":[],"ds":{"scale":0.1978446689001351,"offset":[314.1804840572405,759.900224335752]},"frontendVersion":"1.19.9","links_added_by_ue":[]},"version":0.4} # Paste Redesign.json content here
    else:
        raise ValueError("Invalid workflow_name. Must be 'fill' or 'redesign'.")

    # --- Dynamic Input Injection ---
    # Input Image
    input_image_url = job_input.get("image_url")
    if not input_image_url:
        raise ValueError("An 'image_url' is required for both workflows.")

    # Download image locally
    input_image_path = rp_download(input_image_url) # RunPod utility to download
    
    # Update the workflow JSON with dynamic inputs
    if workflow_name == "fill":
        workflow_json["nodes"][NODE_ID_FILL_IMAGE_LOADER]["widgets_values"][0] = os.path.basename(input_image_path)
        workflow_json["nodes"][NODE_ID_FILL_POSITIVE_PROMPT]["widgets_values"][0] = job_input.get("prompt", "")
        workflow_json["nodes"][NODE_ID_FILL_NEGATIVE_PROMPT]["widgets_values"][0] = job_input.get("negative_prompt", "")
    elif workflow_name == "redesign":
        workflow_json["nodes"][NODE_ID_REDESIGN_IMAGE_LOADER]["widgets_values"][0] = os.path.basename(input_image_path)
        workflow_json["nodes"][NODE_ID_REDESIGN_POSITIVE_PROMPT]["widgets_values"][0] = job_input.get("prompt", "")
        workflow_json["nodes"][NODE_ID_REDESIGN_NEGATIVE_PROMPT]["widgets_values"][0] = job_input.get("negative_prompt", "")
        
        # Denoise Strength (image_to_image_strength)
        denoise_strength = job_input.get("denoise_strength", 0.8) # Default if not provided
        workflow_json["nodes"][NODE_ID_REDESIGN_SAMPLER]["widgets_values"][5] = denoise_strength # Assuming widgets_values[4] is denoise

        # ControlNet Strength
        controlnet_strength = job_input.get("controlnet_strength", 0.8) # Default if not provided
        # Find the ControlNetApply node and set its strength
        # Based on Redesign.json, Node 51 is ApplyControlNet
        workflow_json["nodes"][NODE_ID_REDESIGN_CONTROLNET_APPLY]["widgets_values"][0] = controlnet_strength # Assuming widgets_values[0] is strength


    # --- Setup ComfyUI API call ---
    client = ComfyUIAPIClient("http://127.0.0.1:8188")

    # The ComfyUI API expects a prompt object, which is your workflow JSON
    prompt = workflow_json
    
    # Queue the prompt
    # The RunPod ComfyUI worker template's client already handles the queueing and result fetching
    # You might need to adjust based on the exact implementation in the template's ComfyUIAPIClient.
    # The default worker template already processes and returns the output via `rp_upload` if you configure S3.
    
    # If the `run_inference` function already has logic to run a workflow and save results:
    # We need to make sure the output saving logic uses our R2 function.
    # The template typically generates images into /tmp/outputs and then uploads.
    
    # --- Execute and Process Results ---
    # Assuming the client.run_workflow(prompt) is implemented to:
    # 1. Send the workflow to ComfyUI
    # 2. Poll for results
    # 3. Retrieve generated images from ComfyUI's output folder
    # 4. Return paths to these images.

    # This part might already be handled by the base worker-comfyui template.
    # We need to find where the output images are processed and modify it to upload to R2.
    # Look for a loop that iterates through `images` in `handler.py` around the `rp_upload` call.

    # Example adjustment to output handling (this is a common pattern in RunPod workers):
    # Search for "output" in handler.py and modify the section where images are saved/uploaded.

    # Placeholder for the actual execution.
    # The `worker-comfyui` template's `run_inference` function should already have:
    # 1. `client.queue_prompt(prompt)`
    # 2. Polling for execution status.
    # 3. Fetching results from ComfyUI.
    # 4. Saving results to `job_output_path`.
    # 5. `rp_upload(f"{job_output_path}/image.png")` etc.

    # We need to modify where `rp_upload` is called. Instead, we'll use `upload_to_r2`.

    # Example of how the output handling *might* look, and how to change it:
    # (This is illustrative, adapt to the actual template's output loop)

    # original_images = [] # If the original template returns a list of local file paths
    # for img_path in original_images:
    #     with open(img_path, "rb") as f:
    #         img_bytes = f.read()
    #     # Generate a unique key for R2
    #     r2_key = f"{job['id']}/{os.path.basename(img_path)}"
    #     r2_url = upload_to_r2(img_bytes, r2_key)
    #     if r2_url:
    #         results.append({"image": r2_url})
    #     else:
    #         # Fallback to base64 if R2 upload fails or not configured
    #         results.append({"image": base64.b64encode(img_bytes).decode("utf-8")})
    # rp_cleanup([input_image_path]) # Clean up downloaded input image

    # Let's assume the existing ComfyUIAPIClient.run_workflow already handles the execution
    # and returns a list of base64 images or file paths from the ComfyUI output folder.
    # We need to ensure that the images are caught BEFORE they are deleted and uploaded to R2.

    # **CRITICAL:** The `worker-comfyui` template already has a sophisticated `ComfyUIAPIClient`.
    # You need to modify the `ComfyUIAPIClient` class methods, specifically `get_image` and
    # `run_workflow` or how the results are processed in `run_inference` to use R2.

    # A better approach: Modify the `get_images` or `get_history_output` part of `ComfyUIAPIClient`
    # or the `process_output` logic in `handler.py` to upload to R2.
    # The `ComfyUIAPIClient` in the `worker-comfyui` repo fetches the images directly from ComfyUI's
    # history. We can intercept these bytes and upload them to R2.

    # Let's find the `ComfyUIAPIClient` class in `handler.py`
    # Look for the `get_images` method within that class. It typically saves to `/tmp/outputs`.
    # Modify `get_images` in `ComfyUIAPIClient` to upload to R2 instead of saving locally or
    # return the byte stream for `run_inference` to handle.

    # Given the existing template, the easiest way is to modify the part where outputs are handled
    # after `ComfyUIAPIClient().run_workflow(prompt)` returns.
    
    # Original logic (from typical comfyui worker):
    # results = client.run_workflow(prompt) # This often returns list of local paths or base64
    # final_output = []
    # for result_path in results: # If results are local paths
    #     uploaded_url = rp_upload(result_path)
    #     final_output.append({"image": uploaded_url})
    # rp_cleanup([input_image_path])
    # return {"output": final_output}

    # **Revised Logic for R2 Upload:**
    # In `run_inference` after the `rp_download` for the input image:
    
    # Use the existing RunPod ComfyUI client:
    # It seems the `ComfyUIAPIClient` will handle most of the direct ComfyUI interaction.
    # We need to ensure its `run_workflow` method or whatever returns images,
    # allows us to upload to R2 instead of just saving locally or returning base64.

    # The `runpod-workers/worker-comfyui` template's `ComfyUIAPIClient`
    # `run_workflow` method typically returns a list of local file paths.
    # We need to modify this part:

    # Find the `run_workflow` function in the `ComfyUIAPIClient` class (or similar function that returns images).
    # It likely looks something like this (simplified):
    # class ComfyUIAPIClient:
    #     def run_workflow(self, workflow_json):
    #         # ... queue prompt, get history ...
    #         images = self.get_images(history[output_node_id]) # Example call
    #         return images # This returns local file paths

    # We want to change the `get_images` method or where it's called to upload to R2.

    # Let's assume for now, `client.run_workflow(prompt)` returns a list of base64 strings
    # or local file paths that need to be uploaded to R2.

    # --- Final Output Processing in `run_inference` ---
    try:
        # This calls the ComfyUIAPIClient to execute the workflow
        # The `runpod-workers/worker-comfyui` template's `run_workflow`
        # method already handles fetching images and returning their paths.
        # It typically saves them to `/tmp/outputs`.
        
        # Modify the `run_workflow` in `ComfyUIAPIClient` class itself:
        # Instead of `rp_upload(output_path)` there, call our `upload_to_r2`.
        
        # Go to the `ComfyUIAPIClient` class in `handler.py`.
        # Find the method that processes outputs (e.g., `get_images_from_history`).
        # It will have a loop like this:
        # for node_id in history[prompt_id]['outputs']:
        #     for image in history[prompt_id]['outputs'][node_id]['images']:
        #         image_data = self.get_image(image['filename'], image['subfolder'], image['type'])
        #         # This is where the image_data (bytes) is obtained.
        #         # Instead of saving to a file and then rp_upload, upload directly to R2.
        
        # --- Modifying ComfyUIAPIClient.get_image ---
        # Locate the `get_image` method in `ComfyUIAPIClient`.
        # The current implementation returns a `base64` string. We want to return the raw bytes
        # or handle the upload directly within `get_image`.
        
        # A better strategy: `run_workflow` will collect the local paths, then `run_inference` will handle R2 upload.
        # This keeps `ComfyUIAPIClient` more focused on ComfyUI interaction.
        
        # So, the original `run_inference` output processing:
        # The runpod-workers/worker-comfyui handler.py already has this:
        # `output_paths = client.run_workflow(workflow)` where `output_paths` is a list of image paths like `/tmp/outputs/image.png`
        # and then a loop like:
        # `for output_path in output_paths:`
        #     `results.append(rp_upload(output_path))`
        
        # We need to change `rp_upload(output_path)` to `upload_to_r2`.
        # This implies `rp_upload` must be overridden or used conditionally.
        
        # The `runpod.serverless.utils.rp_upload` function *can* upload to S3 if configured.
        # Let's check `runpod.serverless.utils.rp_upload` source or assume it uses standard boto3 env vars.
        # It does! If `RUNPOD_AWS_ACCESS_KEY_ID`, `RUNPOD_AWS_SECRET_ACCESS_KEY`, `RUNPOD_S3_ENDPOINT`, `RUNPOD_S3_BUCKET`
        # are set, `rp_upload` will use S3. This is the **easiest path**.

        # So, instead of writing our own `upload_to_r2` function and custom logic,
        # we just need to pass the Cloudflare R2 credentials as standard RunPod S3 environment variables!
        
        # --- REVISED PLAN FOR R2 UPLOAD ---
        # We don't need a custom `upload_to_r2` function or explicit `boto3` setup in handler.py.
        # RunPod's `rp_upload` utility function will automatically use your S3/R2 credentials
        # if they are provided as environment variables!
        
        # So, the only handler.py changes needed are:
        # 1. Loading the correct workflow JSON based on input.
        # 2. Injecting the dynamic parameters into the workflow JSON.
        # 3. Ensuring the input image is downloaded using `rp_download`.

        # Let's clean up the `handler.py` modifications.

        # --- Simplified `handler.py` Modification ---

        # 1. Imports (no change needed for boto3, rp_upload already imported)
        # 2. No need for `S3_BUCKET_NAME` etc. or `s3_client` setup.
        # 3. No need for `upload_to_r2` function.

        # Inside `run_inference(job)`:

        job_input = job["input"]
        workflow_name = job_input.get("workflow_name", "fill").lower()

        # Load your workflow JSON based on workflow_name
        # It's best to have these JSONs available in your worker's file system,
        # perhaps copied during the Docker build.
        # For this example, let's assume they are located at `/workspace/worker/workflows/fill.json`
        # and `/workspace/worker/workflows/redesign.json`
        
        workflow_path = f"/workspace/worker/workflows/{workflow_name}.json"
        try:
            with open(workflow_path, "r") as f:
                workflow_json = json.load(f)
        except FileNotFoundError:
            raise ValueError(f"Workflow '{workflow_name}' not found at {workflow_path}")

        # Input Image Download
        input_image_url = job_input.get("image_url")
        if not input_image_url:
            raise ValueError("An 'image_url' is required.")
        input_image_path = rp_download(input_image_url) # Downloads to /tmp/

        # IMPORTANT: ComfyUI loads images by filename. We need to copy the downloaded image
        # into ComfyUI's input folder or tell ComfyUI where to find it.
        # The easiest is to copy it.
        comfyui_input_dir = "/workspace/ComfyUI/input" # This is where ComfyUI expects input images by default
        shutil.copy(input_image_path, comfyui_input_dir)
        input_image_filename = os.path.basename(input_image_path)

        # Map the node IDs for your workflows.
        # It's good practice to define these as constants for clarity.
        NODE_ID_FILL_IMAGE_LOADER = "57" # Example, check your JSON
        NODE_ID_FILL_POSITIVE_PROMPT = "43"
        NODE_ID_FILL_NEGATIVE_PROMPT = "11"

        NODE_ID_REDESIGN_IMAGE_LOADER = "14" # Example, check your JSON
        NODE_ID_REDESIGN_POSITIVE_PROMPT = "63"
        NODE_ID_REDESIGN_NEGATIVE_PROMPT = "7"
        NODE_ID_REDESIGN_SAMPLER = "27"
        NODE_ID_REDESIGN_CONTROLNET_APPLY = "28" # Assuming this is where strength is set

        # Dynamic Input Injection
        if workflow_name == "fill":
            workflow_json["nodes"][NODE_ID_FILL_IMAGE_LOADER]["widgets_values"][0] = input_image_filename
            workflow_json["nodes"][NODE_ID_FILL_POSITIVE_PROMPT]["widgets_values"][0] = job_input.get("prompt", "")
            workflow_json["nodes"][NODE_ID_FILL_NEGATIVE_PROMPT]["widgets_values"][0] = job_input.get("negative_prompt", "")
        elif workflow_name == "redesign":
            workflow_json["nodes"][NODE_ID_REDESIGN_IMAGE_LOADER]["widgets_values"][0] = input_image_filename
            workflow_json["nodes"][NODE_ID_REDESIGN_POSITIVE_PROMPT]["widgets_values"][0] = job_input.get("prompt", "")
            workflow_json["nodes"][NODE_ID_REDESIGN_NEGATIVE_PROMPT]["widgets_values"][0] = job_input.get("negative_prompt", "")
            
            # Denoise Strength (image_to_image_strength)
            denoise_strength = job_input.get("denoise_strength", 0.8)
            workflow_json["nodes"][NODE_ID_REDESIGN_SAMPLER]["widgets_values"][5] = denoise_strength # Adjust index if needed based on your JSON

            # ControlNet Strength
            controlnet_strength = job_input.get("controlnet_strength", 0.8)
            workflow_json["nodes"][NODE_ID_REDESIGN_CONTROLNET_APPLY]["widgets_values"][0] = controlnet_strength # Adjust index if needed

        # Now, call the existing ComfyUI client to run the workflow
        client = ComfyUIAPIClient("http://127.0.0.1:8188")
        
        # The `run_workflow` method will execute the workflow and return a list of local output image paths.
        # These paths will be in the `/tmp/outputs` directory on the worker.
        output_image_paths = await client.run_workflow(workflow_json)

        # ... (inside run_inference function, after client.run_workflow(workflow_json)) ...

        output_image_paths = await client.run_workflow(workflow_json)

        results = []
        for img_path in output_image_paths:
            # --- Changed from rp_upload to custom Cloudflare Images upload ---
            uploaded_url = upload_to_cloudflare_images(img_path)
            if uploaded_url:
                results.append({"image": uploaded_url})
            else:
                # Fallback to base64 if Cloudflare Images upload fails
                print(f"Cloudflare Images upload failed for {img_path}, falling back to base64.")
                with open(img_path, "rb") as f:
                    results.append({"image": base64.b64encode(f.read()).decode("utf-8")})

        return {"output": results}

# ... (rest of the run_inference function, including error handling and cleanup) ...

        # Upload outputs to R2 using RunPod's utility and prepare the response
        results = []
        for img_path in output_image_paths:
            # rp_upload will now automatically upload to your R2 bucket
            # because we will set the required environment variables in RunPod.
            uploaded_url = rp_upload(img_path)
            if uploaded_url:
                results.append({"image": uploaded_url})
            else:
                # Fallback to base64 if R2 upload fails (though it should work if env vars are set)
                with open(img_path, "rb") as f:
                    results.append({"image": base64.b64encode(f.read()).decode("utf-8")})

        # Clean up downloaded input image and generated output images from /tmp
        rp_cleanup([input_image_path] + output_image_paths)

        return {"output": results}

    except Exception as e:
        rp_cleanup([input_image_path] if 'input_image_path' in locals() else [])
        raise e

    ws = None
    client_id = str(uuid.uuid4())
    prompt_id = None
    output_data = []
    errors = []

    try:
        # Establish WebSocket connection
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        print(f"worker-comfyui - Connecting to websocket: {ws_url}")
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        print(f"worker-comfyui - Websocket connected")

        # Queue the workflow
        try:
            queued_workflow = queue_workflow(workflow, client_id)
            prompt_id = queued_workflow.get("prompt_id")
            if not prompt_id:
                raise ValueError(
                    f"Missing 'prompt_id' in queue response: {queued_workflow}"
                )
            print(f"worker-comfyui - Queued workflow with ID: {prompt_id}")
        except requests.RequestException as e:
            print(f"worker-comfyui - Error queuing workflow: {e}")
            raise ValueError(f"Error queuing workflow: {e}")
        except Exception as e:
            print(f"worker-comfyui - Unexpected error queuing workflow: {e}")
            # For ValueError exceptions from queue_workflow, pass through the original message
            if isinstance(e, ValueError):
                raise e
            else:
                raise ValueError(f"Unexpected error queuing workflow: {e}")

        # Wait for execution completion via WebSocket
        print(f"worker-comfyui - Waiting for workflow execution ({prompt_id})...")
        execution_done = False
        while True:
            try:
                out = ws.recv()
                if isinstance(out, str):
                    message = json.loads(out)
                    if message.get("type") == "status":
                        status_data = message.get("data", {}).get("status", {})
                        print(
                            f"worker-comfyui - Status update: {status_data.get('exec_info', {}).get('queue_remaining', 'N/A')} items remaining in queue"
                        )
                    elif message.get("type") == "executing":
                        data = message.get("data", {})
                        if (
                            data.get("node") is None
                            and data.get("prompt_id") == prompt_id
                        ):
                            print(
                                f"worker-comfyui - Execution finished for prompt {prompt_id}"
                            )
                            execution_done = True
                            break
                    elif message.get("type") == "execution_error":
                        data = message.get("data", {})
                        if data.get("prompt_id") == prompt_id:
                            error_details = f"Node Type: {data.get('node_type')}, Node ID: {data.get('node_id')}, Message: {data.get('exception_message')}"
                            print(
                                f"worker-comfyui - Execution error received: {error_details}"
                            )
                            errors.append(f"Workflow execution error: {error_details}")
                            break
                else:
                    continue
            except websocket.WebSocketTimeoutException:
                print(f"worker-comfyui - Websocket receive timed out. Still waiting...")
                continue
            except websocket.WebSocketConnectionClosedException as closed_err:
                try:
                    # Attempt to reconnect
                    ws = _attempt_websocket_reconnect(
                        ws_url,
                        WEBSOCKET_RECONNECT_ATTEMPTS,
                        WEBSOCKET_RECONNECT_DELAY_S,
                        closed_err,
                    )

                    print(
                        "worker-comfyui - Resuming message listening after successful reconnect."
                    )
                    continue
                except (
                    websocket.WebSocketConnectionClosedException
                ) as reconn_failed_err:
                    # If _attempt_websocket_reconnect fails, it raises this exception
                    # Let this exception propagate to the outer handler's except block
                    raise reconn_failed_err

            except json.JSONDecodeError:
                print(f"worker-comfyui - Received invalid JSON message via websocket.")

        if not execution_done and not errors:
            raise ValueError(
                "Workflow monitoring loop exited without confirmation of completion or error."
            )

        # Fetch history even if there were execution errors, some outputs might exist
        print(f"worker-comfyui - Fetching history for prompt {prompt_id}...")
        history = get_history(prompt_id)

        if prompt_id not in history:
            error_msg = f"Prompt ID {prompt_id} not found in history after execution."
            print(f"worker-comfyui - {error_msg}")
            if not errors:
                return {"error": error_msg}
            else:
                errors.append(error_msg)
                return {
                    "error": "Job processing failed, prompt ID not found in history.",
                    "details": errors,
                }

        prompt_history = history.get(prompt_id, {})
        outputs = prompt_history.get("outputs", {})

        if not outputs:
            warning_msg = f"No outputs found in history for prompt {prompt_id}."
            print(f"worker-comfyui - {warning_msg}")
            if not errors:
                errors.append(warning_msg)

        print(f"worker-comfyui - Processing {len(outputs)} output nodes...")
        for node_id, node_output in outputs.items():
            if "images" in node_output:
                print(
                    f"worker-comfyui - Node {node_id} contains {len(node_output['images'])} image(s)"
                )
                for image_info in node_output["images"]:
                    filename = image_info.get("filename")
                    subfolder = image_info.get("subfolder", "")
                    img_type = image_info.get("type")

                    # skip temp images
                    if img_type == "temp":
                        print(
                            f"worker-comfyui - Skipping image {filename} because type is 'temp'"
                        )
                        continue

                    if not filename:
                        warn_msg = f"Skipping image in node {node_id} due to missing filename: {image_info}"
                        print(f"worker-comfyui - {warn_msg}")
                        errors.append(warn_msg)
                        continue

                    image_bytes = get_image_data(filename, subfolder, img_type)

                    if image_bytes:
                        file_extension = os.path.splitext(filename)[1] or ".png"

                        if os.environ.get("BUCKET_ENDPOINT_URL"):
                            try:
                                with tempfile.NamedTemporaryFile(
                                    suffix=file_extension, delete=False
                                ) as temp_file:
                                    temp_file.write(image_bytes)
                                    temp_file_path = temp_file.name
                                print(
                                    f"worker-comfyui - Wrote image bytes to temporary file: {temp_file_path}"
                                )

                                print(f"worker-comfyui - Uploading {filename} to S3...")
                                s3_url = rp_upload.upload_image(job_id, temp_file_path)
                                os.remove(temp_file_path)  # Clean up temp file
                                print(
                                    f"worker-comfyui - Uploaded {filename} to S3: {s3_url}"
                                )
                                # Append dictionary with filename and URL
                                output_data.append(
                                    {
                                        "filename": filename,
                                        "type": "s3_url",
                                        "data": s3_url,
                                    }
                                )
                            except Exception as e:
                                error_msg = f"Error uploading {filename} to S3: {e}"
                                print(f"worker-comfyui - {error_msg}")
                                errors.append(error_msg)
                                if "temp_file_path" in locals() and os.path.exists(
                                    temp_file_path
                                ):
                                    try:
                                        os.remove(temp_file_path)
                                    except OSError as rm_err:
                                        print(
                                            f"worker-comfyui - Error removing temp file {temp_file_path}: {rm_err}"
                                        )
                        else:
                            # Return as base64 string
                            try:
                                base64_image = base64.b64encode(image_bytes).decode(
                                    "utf-8"
                                )
                                # Append dictionary with filename and base64 data
                                output_data.append(
                                    {
                                        "filename": filename,
                                        "type": "base64",
                                        "data": base64_image,
                                    }
                                )
                                print(f"worker-comfyui - Encoded {filename} as base64")
                            except Exception as e:
                                error_msg = f"Error encoding {filename} to base64: {e}"
                                print(f"worker-comfyui - {error_msg}")
                                errors.append(error_msg)
                    else:
                        error_msg = f"Failed to fetch image data for {filename} from /view endpoint."
                        errors.append(error_msg)

            # Check for other output types
            other_keys = [k for k in node_output.keys() if k != "images"]
            if other_keys:
                warn_msg = (
                    f"Node {node_id} produced unhandled output keys: {other_keys}."
                )
                print(f"worker-comfyui - WARNING: {warn_msg}")
                print(
                    f"worker-comfyui - --> If this output is useful, please consider opening an issue on GitHub to discuss adding support."
                )

    except websocket.WebSocketException as e:
        print(f"worker-comfyui - WebSocket Error: {e}")
        print(traceback.format_exc())
        return {"error": f"WebSocket communication error: {e}"}
    except requests.RequestException as e:
        print(f"worker-comfyui - HTTP Request Error: {e}")
        print(traceback.format_exc())
        return {"error": f"HTTP communication error with ComfyUI: {e}"}
    except ValueError as e:
        print(f"worker-comfyui - Value Error: {e}")
        print(traceback.format_exc())
        return {"error": str(e)}
    except Exception as e:
        print(f"worker-comfyui - Unexpected Handler Error: {e}")
        print(traceback.format_exc())
        return {"error": f"An unexpected error occurred: {e}"}
    finally:
        if ws and ws.connected:
            print(f"worker-comfyui - Closing websocket connection.")
            ws.close()

    final_result = {}

    if output_data:
        final_result["images"] = output_data

    if errors:
        final_result["errors"] = errors
        print(f"worker-comfyui - Job completed with errors/warnings: {errors}")

    if not output_data and errors:
        print(f"worker-comfyui - Job failed with no output images.")
        return {
            "error": "Job processing failed",
            "details": errors,
        }
    elif not output_data and not errors:
        print(
            f"worker-comfyui - Job completed successfully, but the workflow produced no images."
        )
        final_result["status"] = "success_no_images"
        final_result["images"] = []

    print(f"worker-comfyui - Job completed. Returning {len(output_data)} image(s).")
    return final_result


if __name__ == "__main__":
    print("worker-comfyui - Starting handler...")
    runpod.serverless.start({"handler": handler})
