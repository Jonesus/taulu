import os
import json
import datetime
import threading
import logging
from io import BytesIO
from flask import Flask, jsonify, request, Response
from dotenv import load_dotenv

from immich import ImmichClient
from prepare import convert_image_to_bin

load_dotenv()

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S'
)
logger = logging.getLogger(__name__)

WIDTH = 1200
HEIGHT = 1600
READY_DIR = os.path.join(os.path.dirname(__file__), 'state')
STATE_FILE = os.path.join(os.path.dirname(__file__), 'state', 'state.json')
PEOPLE_IDS_FILE = os.path.join(os.path.dirname(__file__), 'people-ids.json')

SLEEP_MINUTES = os.getenv("SLEEP_MINUTES")
REFRESH_HOUR = os.getenv("REFRESH_HOUR")

os.makedirs(READY_DIR, exist_ok=True)

try:
    with open(PEOPLE_IDS_FILE, 'r') as f:
        PEOPLE_IDS = json.load(f)
except FileNotFoundError:
    logger.warning(f"{PEOPLE_IDS_FILE} not found, using empty list")
    PEOPLE_IDS = []

IMMICH_API_KEY = os.getenv("IMMICH_API_KEY")
IMMICH_BASE_URL = os.getenv("IMMICH_BASE_URL")
immich_client = ImmichClient(api_key=IMMICH_API_KEY, base_url=IMMICH_BASE_URL) if IMMICH_API_KEY else None

# ---------------------------------------------------------------------------
# Image manager
#   images[0]  = daily image, refreshed once per day
#   images[1]  = navigation buffer
#   images[2]  = navigation buffer
# ---------------------------------------------------------------------------

NUM_SLOTS = 3

class ImageManager:
    def __init__(self):
        self.images = [None] * NUM_SLOTS  # {'id': str, 'path': str} or None
        self.next_daily = None            # pre-fetched image ready for the next day
        self.current_index = 0
        self.last_date = None
        self.fetching = set()             # slot indices (int or 'next_daily') being fetched
        self.shown_ids = set()            # asset IDs that have been served via /api/image.bin
        self.lock = threading.Lock()
        self._load_state()

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            raw = state.get('images', [])
            self.images = [
                (img if img and os.path.exists(img.get('path', '')) else None)
                for img in (raw + [None] * NUM_SLOTS)[:NUM_SLOTS]
            ]
            nd = state.get('nextDaily')
            self.next_daily = nd if nd and os.path.exists(nd.get('path', '')) else None
            self.current_index = state.get('currentIndex', 0) % NUM_SLOTS
            self.last_date = state.get('lastDate')
            self.shown_ids = set(state.get('shownIds', []))
        except Exception as e:
            logger.error(f"Error loading state: {e}")

    def _save_state(self):
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'images': self.images,
                    'nextDaily': self.next_daily,
                    'currentIndex': self.current_index,
                    'lastDate': self.last_date,
                    'shownIds': list(self.shown_ids),
                }, f)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def _fetch(self, slot):
        """Fetch a random image into `slot` (int index or 'next_daily'). Background thread."""
        try:
            if not immich_client:
                logger.warning("Immich client not configured")
                return
            with self.lock:
                exclude = set(self.shown_ids)
            asset = immich_client.find_random_group_photo(PEOPLE_IDS, exclude_ids=exclude)
            if asset is None and exclude:
                # All known candidates have been shown — reset history and try again
                logger.info(f"All {len(exclude)} shown candidates exhausted, resetting history")
                with self.lock:
                    self.shown_ids.clear()
                    self._save_state()
                asset = immich_client.find_random_group_photo(PEOPLE_IDS)
            if not asset:
                logger.warning(f"No asset found for slot {slot}")
                return
            data = immich_client.download_asset(asset['id'])
            if not data:
                logger.warning(f"Download failed for {asset['id']} (slot {slot})")
                return
            path = os.path.join(READY_DIR, f"{asset['id']}.bin")
            with open(path, 'wb') as f:
                f.write(convert_image_to_bin(BytesIO(data)))
            with self.lock:
                if slot == 'next_daily':
                    self.next_daily = {'id': asset['id'], 'path': path}
                else:
                    self.images[slot] = {'id': asset['id'], 'path': path}
                self.fetching.discard(slot)
                self._save_state()
            logger.info(f"Slot {slot} ready: {asset['id']}")
        except Exception as e:
            logger.error(f"Error fetching slot {slot}: {e}")
            with self.lock:
                self.fetching.discard(slot)

    def _start_fetch(self, slot):
        """Queue a background fetch for `slot`. Must be called while holding lock."""
        if slot in self.fetching:
            return
        self.fetching.add(slot)
        threading.Thread(target=self._fetch, args=(slot,), daemon=True).start()
        logger.info(f"Queued fetch for slot {slot}")

    def ensure_images(self):
        """Called on each request: swap in pre-fetched image on day change, fill empty slots."""
        today = datetime.date.today().isoformat()
        with self.lock:
            if self.last_date != today:
                logger.info(f"New day ({today}), swapping in pre-fetched daily image")
                self.images[0] = self.next_daily  # instant swap; None if not ready yet
                self.current_index = 0
                self.next_daily = None
                self.last_date = today
                self._save_state()
            for i in range(NUM_SLOTS):
                if self.images[i] is None:
                    self._start_fetch(i)
            if self.next_daily is None:
                self._start_fetch('next_daily')

    def handle_action(self, action):
        """Advance or retreat current_index. Buffer slots are replaced in the background."""
        with self.lock:
            if action == 'next':
                self.current_index = (self.current_index + 1) % NUM_SLOTS
            elif action == 'previous':
                self.current_index = (self.current_index - 1) % NUM_SLOTS
            else:
                return
            self._save_state()
            logger.info(f"Action '{action}': now on slot {self.current_index}")
            if self.current_index != 0:
                self._start_fetch(self.current_index)


manager = ImageManager()
manager.ensure_images()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/health', methods=['GET'])
def health():
    """Kubernetes liveness probe."""
    return jsonify({"status": "healthy"}), 200

@app.route('/ready', methods=['GET'])
def ready():
    """Kubernetes readiness probe."""
    with manager.lock:
        has_image = manager.images[0] is not None
        is_fetching = 0 in manager.fetching
    if has_image or is_fetching:
        return jsonify({"status": "ready", "hasImages": has_image, "updating": is_fetching}), 200
    return jsonify({"status": "not ready", "reason": "no images available"}), 503


@app.route('/api/current.json', methods=['GET'])
def get_current():
    logger.info(f"GET /api/current.json from {request.remote_addr}")
    manager.ensure_images()

    with manager.lock:
        image = manager.images[manager.current_index]
        has_image = manager.images[0] is not None
        updating = bool(manager.fetching)
        image_count = sum(1 for img in manager.images if img is not None)
        current_image_id = image['id'] if image else "no-image"

    now = datetime.datetime.now()
    if SLEEP_MINUTES:
        try:
            sleep_duration_us = int(float(SLEEP_MINUTES) * 60 * 1_000_000)
        except ValueError:
            logger.warning(f"Invalid SLEEP_MINUTES: {SLEEP_MINUTES}, defaulting to 1 hour")
            sleep_duration_us = 3600 * 1_000_000
    elif REFRESH_HOUR is not None:
        try:
            refresh_hour = int(REFRESH_HOUR)
            if not 0 <= refresh_hour <= 23:
                raise ValueError("REFRESH_HOUR must be 0-23")
            refresh_time = now.replace(hour=refresh_hour, minute=0, second=0, microsecond=0)
            if refresh_time <= now:
                refresh_time += datetime.timedelta(days=1)
            sleep_duration_us = int((refresh_time - now).total_seconds() * 1_000_000)
        except ValueError as e:
            logger.warning(f"Invalid REFRESH_HOUR: {REFRESH_HOUR} - {e}, defaulting to 1 hour")
            sleep_duration_us = 3600 * 1_000_000
    else:
        next_hour = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        sleep_duration_us = int((next_hour - now).total_seconds() * 1_000_000)

    logger.info(f"Responding with imageId={current_image_id}, sleep={sleep_duration_us // 1_000_000}s")
    return jsonify({
        "imageId": current_image_id,
        "sleepDuration": sleep_duration_us,
        "hasImage": has_image,
        "imageCount": image_count,
        "updating": updating,
        "devServerHost": None
    })


@app.route('/api/image.bin', methods=['GET'])
def get_image():
    logger.info(f"GET /api/image.bin from {request.remote_addr}")

    with manager.lock:
        image = manager.images[manager.current_index] or manager.images[0]

    if not image:
        logger.warning("No image available")
        return "No image ready", 404
    if not os.path.exists(image['path']):
        logger.warning(f"Image file not found: {image['path']}")
        return "Image file not found", 404

    logger.info(f"Serving {image['id']} ({os.path.getsize(image['path'])} bytes)")
    with manager.lock:
        if image['id'] not in manager.shown_ids:
            manager.shown_ids.add(image['id'])
            manager._save_state()
            logger.info(f"Marked {image['id']} as shown ({len(manager.shown_ids)} total)")
    with open(image['path'], 'rb') as f:
        return Response(f.read(), mimetype='application/octet-stream')


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


if __name__ == '__main__':
    logger.info("Starting server at http://0.0.0.0:3000")
    app.run(host='0.0.0.0', port=3000, debug=True)
