import os
import json
import datetime
import threading
import logging
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

    def _update_image(self):
        """Internal method that performs the actual image update (runs in background thread)"""
        today = datetime.date.today().isoformat()

        try:
            print(f"Updating images for {today}...")

            if not immich_client:
                print("Immich client not initialized (missing API key).")
                return

            # Update the date at the start
            with self.update_lock:
                self.last_update_date = today
                self.current_index = 0
                self.save_state()

            # Download up to 3 images, replacing them one by one
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

                # Update state immediately after each image (thread-safe)
                # Replace existing image at index i, or append if list is shorter
                new_image = {
                    'id': asset['id'],
                    'path': filepath
                }
                with self.update_lock:
                    if i < len(self.daily_images):
                        self.daily_images[i] = new_image
                    else:
                        self.daily_images.append(new_image)
                    self.save_state()

                print(f"Successfully processed image {i+1}: {asset['id']}")

            # Final summary
            with self.update_lock:
                image_count = len(self.daily_images)
                if image_count > 0:
                    print(f"Successfully updated with {image_count} image(s)")
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

    # Get the next image in rotation
    image = manager.get_next_image()

    if not image or not os.path.exists(image['path']):
        logger.warning(f"No image available for {request.remote_addr}")
        return "No image ready", 404

    logger.info(f"Serving image {image['id']} ({os.path.getsize(image['path'])} bytes)")

    with open(image['path'], 'rb') as f:
        data = f.read()

    return Response(data, mimetype='application/octet-stream')

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