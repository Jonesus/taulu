import os
import json
import datetime
import threading
import logging
from io import BytesIO
from flask import Flask, jsonify, request, Response
from dotenv import load_dotenv

# Local imports
from immich import ImmichClient
from prepare import convert_image_to_bin

load_dotenv()

app = Flask(__name__)

# Configure logging with timestamps
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration
WIDTH = 1200
HEIGHT = 1600
READY_DIR = os.path.join(os.path.dirname(__file__), 'ready')
STATE_FILE = os.path.join(os.path.dirname(__file__), 'state.json')
PEOPLE_IDS_FILE = os.path.join(os.path.dirname(__file__), 'people-ids.json')

# Sleep configuration - choose ONE of:
# SLEEP_MINUTES: Fixed sleep duration in minutes between image refreshes
# REFRESH_HOUR: Hour of day (0-23) when display should wake to refresh (defaults to hourly refresh)
SLEEP_MINUTES = os.getenv("SLEEP_MINUTES")
REFRESH_HOUR = os.getenv("REFRESH_HOUR")

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

    def _fetch_and_store_image(self, slot_index):
        """Fetch one random photo and store it in slot_index. Returns True on success."""
        asset = immich_client.find_random_group_photo(PEOPLE_IDS)
        if not asset:
            print(f"No suitable asset found for slot {slot_index}.")
            return False

        image_data = immich_client.download_asset(asset['id'])
        if not image_data:
            print(f"Failed to download asset {asset['id']}.")
            return False

        processed_data = convert_image_to_bin(BytesIO(image_data))
        filepath = os.path.join(READY_DIR, f"{asset['id']}.bin")

        with open(filepath, 'wb') as f:
            f.write(processed_data)

        new_image = {'id': asset['id'], 'path': filepath}
        with self.update_lock:
            if slot_index < len(self.daily_images):
                self.daily_images[slot_index] = new_image
            else:
                self.daily_images.append(new_image)
            self.save_state()

        print(f"Slot {slot_index} filled with {asset['id']}")
        return True

    def _update_image(self):
        """Fetch 3 fresh images from scratch (used on first boot / no images). Runs in background thread."""
        today = datetime.date.today().isoformat()

        try:
            print(f"Fetching initial 3 images for {today}...")

            if not immich_client:
                print("Immich client not initialized (missing API key).")
                return

            # Mark date and reset index at the start to prevent duplicate triggers
            with self.update_lock:
                self.last_update_date = today
                self.current_index = 0
                self.save_state()

            for i in range(3):
                print(f"Fetching image {i+1}/3...")
                self._fetch_and_store_image(i)

            with self.update_lock:
                print(f"Initial fetch done: {len(self.daily_images)} image(s) available")

        except Exception as e:
            print(f"Error fetching images: {e}")
        finally:
            with self.update_lock:
                self.updating = False

    def _fetch_image_for_slot(self, slot_index):
        """Fetch one new image and replace the given slot. Runs in background thread."""
        try:
            if not immich_client:
                return

            print(f"Fetching new image for slot {slot_index}...")
            self._fetch_and_store_image(slot_index)

        except Exception as e:
            print(f"Error fetching image for slot {slot_index}: {e}")
        finally:
            with self.update_lock:
                self.updating = False

    def ensure_daily_image(self):
        """Trigger image fetch if needed (non-blocking).

        - No images at all: fetch 3 fresh images.
        - Day changed: replace only the last slot (rolling window).
        - Same day with images: no-op.
        """
        today = datetime.date.today().isoformat()

        with self.update_lock:
            has_images = len(self.daily_images) > 0

            if self.updating:
                return

            if not has_images:
                # First boot or state wiped — fetch 3 from scratch
                self.updating = True
                thread = threading.Thread(target=self._update_image, daemon=True)
                thread.start()
                print("Started initial image fetch (3 images)...")
                return

            if self.last_update_date == today:
                return  # Already up to date

            # Day changed — replace only the last slot
            self.updating = True
            self.last_update_date = today
            self.save_state()
            slot = len(self.daily_images) - 1

        thread = threading.Thread(target=self._fetch_image_for_slot, args=(slot,), daemon=True)
        thread.start()
        print(f"Day changed, refreshing slot {slot}...")

    def handle_action(self, action):
        """Handle a navigation action from the device.

        - 'next': advance current_index, then replace the vacated slot with a fresh image.
        - 'previous': step back current_index (no fetch).
        - 'refresh': no-op (device re-renders the current image).
        """
        slot_to_replace = None

        with self.update_lock:
            if not self.daily_images:
                return

            if action == 'next':
                old_index = self.current_index
                self.current_index = (self.current_index + 1) % len(self.daily_images)
                self.save_state()
                slot_to_replace = old_index  # Roll in a fresh image for the slot we left
            elif action == 'previous':
                self.current_index = (self.current_index - 1) % len(self.daily_images)
                self.save_state()
            # 'refresh' intentionally does nothing here

            if slot_to_replace is not None and not self.updating:
                self.updating = True
            else:
                slot_to_replace = None  # Already updating or no replacement needed

        if slot_to_replace is not None:
            thread = threading.Thread(
                target=self._fetch_image_for_slot,
                args=(slot_to_replace,),
                daemon=True
            )
            thread.start()
            print(f"Rolling window: fetching fresh image for slot {slot_to_replace}...")

manager = DailyImageManager()
manager.ensure_daily_image()

@app.route('/health', methods=['GET'])
def health():
    """Kubernetes liveness probe - checks if app is alive"""
    return jsonify({"status": "healthy"}), 200

@app.route('/ready', methods=['GET'])
def ready():
    """Kubernetes readiness probe - checks if app is ready to serve traffic"""
    with manager.update_lock:
        has_images = len(manager.daily_images) > 0
        is_updating = manager.updating

    if has_images or is_updating:
        return jsonify({"status": "ready", "hasImages": has_images, "updating": is_updating}), 200
    else:
        return jsonify({"status": "not ready", "reason": "no images available"}), 503

@app.route('/api/current.json', methods=['GET'])
def get_current():
    logger.info(f"GET /api/current.json from {request.remote_addr}")

    # Trigger update check (non-blocking)
    manager.ensure_daily_image()

    with manager.update_lock:
        has_images = len(manager.daily_images) > 0
        updating = manager.updating
        image_count = len(manager.daily_images)
        current_image_id = manager.daily_images[manager.current_index]['id'] if has_images else "no-image"

    # Calculate sleep duration based on configuration
    now = datetime.datetime.now()
    
    if SLEEP_MINUTES:
        # Use fixed sleep duration in minutes
        try:
            sleep_minutes = float(SLEEP_MINUTES)
            sleep_duration_us = int(sleep_minutes * 60 * 1_000_000)
        except ValueError:
            logger.warning(f"Invalid SLEEP_MINUTES value: {SLEEP_MINUTES}, defaulting to 1 hour")
            sleep_duration_us = int(3600 * 1_000_000)
    elif REFRESH_HOUR is not None:
        # Calculate time until specific hour of day
        try:
            refresh_hour = int(REFRESH_HOUR)
            if not 0 <= refresh_hour <= 23:
                raise ValueError("REFRESH_HOUR must be between 0 and 23")
            
            refresh_time = now.replace(hour=refresh_hour, minute=0, second=0, microsecond=0)
            
            # If refresh time has already passed today, schedule for tomorrow
            if refresh_time <= now:
                refresh_time += datetime.timedelta(days=1)
            
            seconds_until_refresh = (refresh_time - now).total_seconds()
            sleep_duration_us = int(seconds_until_refresh * 1_000_000)
        except ValueError as e:
            logger.warning(f"Invalid REFRESH_HOUR value: {REFRESH_HOUR} - {e}, defaulting to 1 hour")
            sleep_duration_us = int(3600 * 1_000_000)
    else:
        # Default: sleep until next exact hour
        next_hour = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        seconds_until_next_hour = (next_hour - now).total_seconds()
        sleep_duration_us = int(seconds_until_next_hour * 1_000_000)

    logger.info(f"Responding with imageId={current_image_id}, sleepDuration={sleep_duration_us/1_000_000:.0f}s, hasImage={has_images}")

    return jsonify({
        "imageId": current_image_id,
        "sleepDuration": sleep_duration_us,
        "hasImage": has_images,
        "imageCount": image_count,
        "updating": updating,
        "devServerHost": None
    })

@app.route('/api/image.bin', methods=['GET'])
def get_image():
    logger.info(f"GET /api/image.bin from {request.remote_addr}")

    # Trigger update check (non-blocking)
    manager.ensure_daily_image()

    # Serve the current image — index is managed by /api/action
    with manager.update_lock:
        if not manager.daily_images:
            logger.warning(f"No image available for {request.remote_addr}")
            return "No image ready", 404
        image = manager.daily_images[manager.current_index]

    if not os.path.exists(image['path']):
        logger.warning(f"Image file not found: {image['path']}")
        return "Image file not found", 404

    logger.info(f"Serving image {image['id']} ({os.path.getsize(image['path'])} bytes)")

    with open(image['path'], 'rb') as f:
        data = f.read()

    return Response(data, mimetype='application/octet-stream')

@app.route('/api/action', methods=['POST'])
def action():
    data = request.json or {}
    act = data.get('action', '')
    device_id = data.get('deviceId', 'unknown')
    logger.info(f"POST /api/action from {request.remote_addr} - {device_id}: {act}")
    manager.handle_action(act)
    return jsonify({"status": "ok"})

@app.route('/api/device-status', methods=['POST'])
def device_status():
    logger.info(f"POST /api/device-status from {request.remote_addr}: {request.json}")
    return jsonify({"status": "received"})

@app.route('/api/logs', methods=['POST'])
def logs():
    data = request.json
    level = data.get('logLevel', 'INFO')
    msg = data.get('logs', '')
    device_id = data.get('deviceId', 'unknown')
    logger.info(f"POST /api/logs from {request.remote_addr} - [{level}] {device_id}: {msg}")
    return jsonify({"status": "logged"})

@app.route('/api/white.bin', methods=['GET'])
def get_white():
    logger.info(f"GET /api/white.bin from {request.remote_addr}")
    # Fallback/Test endpoint
    data = bytes([255, 255, 255]) * (WIDTH * HEIGHT)
    return Response(data, mimetype='application/octet-stream')

if __name__ == '__main__':
    logger.info("Starting server at http://0.0.0.0:3000")
    app.run(host='0.0.0.0', port=3000, debug=True)