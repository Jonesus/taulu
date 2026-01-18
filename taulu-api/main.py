import os
import json
import time
import datetime
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
IMMICH_BASE_URL = os.getenv("IMMICH_BASE_URL", "https://kuvat.palosuo.fi")
immich_client = ImmichClient(api_key=IMMICH_API_KEY, base_url=IMMICH_BASE_URL) if IMMICH_API_KEY else None

class DailyImageManager:
    def __init__(self):
        self.current_image_path = None
        self.current_image_id = None
        self.last_update_date = None
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    self.last_update_date = state.get('date')
                    self.current_image_id = state.get('imageId')
                    self.current_image_path = state.get('path')
                    
                    # Verify file exists
                    if self.current_image_path and not os.path.exists(self.current_image_path):
                        print("Warning: State file references missing image.")
                        self.current_image_path = None
            except Exception as e:
                print(f"Error loading state: {e}")

    def save_state(self):
        state = {
            'date': self.last_update_date,
            'imageId': self.current_image_id,
            'path': self.current_image_path
        }
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            print(f"Error saving state: {e}")

    def ensure_daily_image(self):
        today = datetime.date.today().isoformat()
        
        if self.last_update_date == today and self.current_image_path and os.path.exists(self.current_image_path):
            return # We are good for today

        print(f"Updating image for {today}...")
        
        if not immich_client:
            print("Immich client not initialized (missing API key).")
            return

        # Fetch new image
        asset = immich_client.find_random_group_photo(PEOPLE_IDS)
        if not asset:
            print("No suitable asset found.")
            return

        print(f"Downloading asset {asset['id']}...")
        image_data = immich_client.download_asset(asset['id'])
        if not image_data:
            print("Failed to download asset.")
            return

        # Process image
        print("Processing image...")
        from io import BytesIO
        try:
            processed_data = convert_image_to_bin(BytesIO(image_data))
            
            # Save to file
            filename = f"{asset['id']}.bin"
            filepath = os.path.join(READY_DIR, filename)
            
            with open(filepath, 'wb') as f:
                f.write(processed_data)
                
            # Update state
            # Optional: Clean up old files? For now, we keep them.
            self.current_image_path = filepath
            self.current_image_id = asset['id']
            self.last_update_date = today
            self.save_state()
            print(f"Successfully updated image to {asset['id']}")
            
        except Exception as e:
            print(f"Error processing image: {e}")

manager = DailyImageManager()

@app.route('/api/current.json', methods=['GET'])
def get_current():
    # Trigger update check
    manager.ensure_daily_image()
    
    has_image = bool(manager.current_image_path and os.path.exists(manager.current_image_path))
    
    return jsonify({
        "imageId": manager.current_image_id if has_image else "no-image",
        "sleepDuration": 240_000_000, # Example value
        "hasImage": has_image,
        "devServerHost": None
    })

@app.route('/api/image.bin', methods=['GET'])
def get_image():
    manager.ensure_daily_image()
    
    if not manager.current_image_path or not os.path.exists(manager.current_image_path):
        return "No image ready", 404
        
    with open(manager.current_image_path, 'rb') as f:
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
    # Initial check
    manager.ensure_daily_image()
    print("Starting server at http://0.0.0.0:3000")
    app.run(host='0.0.0.0', port=3000, debug=True)