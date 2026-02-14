import os
import json
import time
import datetime
import threading
from flask import Flask, jsonify, request, Response
from dotenv import load_dotenv

# Local imports
from immich import ImmichClient
from prepare import convert_image_to_bin

load_dotenv()

app = Flask(__name__)

# Configuration
WIDTH = 1200
HEIGHT = 1600
READY_DIR = os.path.join(os.path.dirname(__file__), 'ready')
STATE_FILE = os.path.join(os.path.dirname(__file__), 'state.json')
PEOPLE_IDS_FILE = os.path.join(os.path.dirname(__file__), 'people-ids.json')

# Ensure ready directory exists
os.makedirs(READY_DIR, exist_ok=True)

# Load People IDs
try:
    with open(PEOPLE_IDS_FILE, 'r') as f:
        PEOPLE_IDS = json.load(f)
except FileNotFoundError:
    print(f"Error: {PEOPLE_IDS_FILE} not found.")
    PEOPLE_IDS = []

# Initialize Immich Client
IMMICH_API_KEY = os.getenv("IMMICH_API_KEY")
IMMICH_BASE_URL = os.getenv("IMMICH_BASE_URL")
immich_client = ImmichClient(api_key=IMMICH_API_KEY, base_url=IMMICH_BASE_URL) if IMMICH_API_KEY else None

class DailyImageManager:
    def __init__(self):
        self.daily_images = []  # List of {'id': str, 'path': str}
        self.current_index = 0
        self.last_update_date = None
        self.update_lock = threading.Lock()
        self.updating = False
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    self.last_update_date = state.get('date')
                    self.daily_images = state.get('images', [])
                    self.current_index = state.get('currentIndex', 0)

                    # Verify files exist and filter out missing ones
                    self.daily_images = [
                        img for img in self.daily_images
                        if img.get('path') and os.path.exists(img['path'])
                    ]

                    if len(self.daily_images) == 0:
                        print("Warning: No valid images found in state.")
            except Exception as e:
                print(f"Error loading state: {e}")

    def save_state(self):
        state = {
            'date': self.last_update_date,
            'images': self.daily_images,
            'currentIndex': self.current_index
        }
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            print(f"Error saving state: {e}")

    def _update_image(self):
        """Internal method that performs the actual image update (runs in background thread)"""
        today = datetime.date.today().isoformat()

        try:
            print(f"Updating images for {today}...")

            if not immich_client:
                print("Immich client not initialized (missing API key).")
                return

            new_images = []

            # Download up to 3 images
            for i in range(3):
                print(f"Fetching image {i+1}/3...")

                # Fetch new image
                asset = immich_client.find_random_group_photo(PEOPLE_IDS)
                if not asset:
                    print(f"No suitable asset found for image {i+1}.")
                    continue

                print(f"Downloading asset {asset['id']}...")
                image_data = immich_client.download_asset(asset['id'])
                if not image_data:
                    print(f"Failed to download asset {asset['id']}.")
                    continue

                # Process image
                print(f"Processing image {i+1}...")
                from io import BytesIO
                processed_data = convert_image_to_bin(BytesIO(image_data))

                # Save to file
                filename = f"{asset['id']}.bin"
                filepath = os.path.join(READY_DIR, filename)

                with open(filepath, 'wb') as f:
                    f.write(processed_data)

                new_images.append({
                    'id': asset['id'],
                    'path': filepath
                })

                print(f"Successfully processed image {i+1}: {asset['id']}")

            # Update state (thread-safe)
            with self.update_lock:
                if new_images:
                    # Optional: Clean up old files? For now, we keep them.
                    self.daily_images = new_images
                    self.current_index = 0
                    self.last_update_date = today
                    self.save_state()
                    print(f"Successfully updated with {len(new_images)} image(s)")
                else:
                    print("No images were successfully downloaded.")

        except Exception as e:
            print(f"Error processing images: {e}")
        finally:
            with self.update_lock:
                self.updating = False

    def ensure_daily_image(self):
        """Trigger daily image update if needed (non-blocking)"""
        today = datetime.date.today().isoformat()

        # Check if we already have today's images
        with self.update_lock:
            if self.last_update_date == today and len(self.daily_images) > 0:
                return # We are good for today

            # Check if an update is already in progress
            if self.updating:
                return # Update already running

            # Mark as updating and start background thread
            self.updating = True

        # Start background update
        thread = threading.Thread(target=self._update_image, daemon=True)
        thread.start()
        print("Started background image update...")

    def get_next_image(self):
        """Get the next image in rotation and advance the index"""
        with self.update_lock:
            if len(self.daily_images) == 0:
                return None

            # Get current image
            image = self.daily_images[self.current_index]

            # Advance to next image (circular)
            self.current_index = (self.current_index + 1) % len(self.daily_images)
            self.save_state()

            return image

manager = DailyImageManager()

@app.route('/api/current.json', methods=['GET'])
def get_current():
    # Trigger update check (non-blocking)
    manager.ensure_daily_image()

    with manager.update_lock:
        has_images = len(manager.daily_images) > 0
        updating = manager.updating
        image_count = len(manager.daily_images)
        current_image_id = manager.daily_images[manager.current_index]['id'] if has_images else "no-image"

    return jsonify({
        "imageId": current_image_id,
        "sleepDuration": 240_000_000, # Example value
        "hasImage": has_images,
        "imageCount": image_count,
        "updating": updating,
        "devServerHost": None
    })

@app.route('/api/image.bin', methods=['GET'])
def get_image():
    # Trigger update check (non-blocking)
    manager.ensure_daily_image()

    # Get the next image in rotation
    image = manager.get_next_image()

    if not image or not os.path.exists(image['path']):
        return "No image ready", 404

    with open(image['path'], 'rb') as f:
        data = f.read()

    return Response(data, mimetype='application/octet-stream')

@app.route('/api/time', methods=['GET'])
def get_time():
    return jsonify({
        "epoch": int(time.time() * 1000)
    })

@app.route('/api/device-status', methods=['POST'])
def device_status():
    print(f"\n[DEVICE STATUS] {request.json}")
    return jsonify({"status": "received"})

@app.route('/api/logs', methods=['POST'])
def logs():
    data = request.json
    level = data.get('logLevel', 'INFO')
    msg = data.get('logs', '')
    print(f"[{level}] ESP32: {msg}")
    return jsonify({"status": "logged"})

@app.route('/api/bhutan.bin', methods=['GET'])
def get_bhutan():
    # Fallback/Test endpoint
    data = bytes([255, 255, 255]) * (WIDTH * HEIGHT)
    return Response(data, mimetype='application/octet-stream')

if __name__ == '__main__':
    # Initial check (only in the reloader process to avoid running twice in debug mode)
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        manager.ensure_daily_image()
    print("Starting server at http://0.0.0.0:3000")
    app.run(host='0.0.0.0', port=3000, debug=True)