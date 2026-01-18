import requests
import random
from typing import List, Optional

class ImmichClient:
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.headers = {
            'x-api-key': self.api_key,
            'Accept': 'application/json'
        }

    def search_assets(self, person_ids: List[str]) -> List[dict]:
        """
        Search for assets containing the given person IDs.
        Note: Immich search might be OR based, so we may need to filter client-side.
        """
        endpoint = f"{self.base_url}/api/search/metadata"
        payload = {
            "personIds": person_ids,
            "withExif": True
        }
        
        try:
            response = requests.post(endpoint, json=payload, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            
            # Handle standard Immich search response structure
            if isinstance(data, dict):
                if 'assets' in data and isinstance(data['assets'], dict) and 'items' in data['assets']:
                    return data['assets']['items']
                # Fallback for other potential structures
                return []
            elif isinstance(data, list):
                return data
            
            return []
        except requests.RequestException as e:
            print(f"Error searching assets: {e}")
            # Fallback: Try GET /api/asset if search/metadata fails or returns 404
            # Some versions might use different endpoints.
            return []

    def get_asset_info(self, asset_id: str) -> Optional[dict]:
        """Get detailed info for an asset, including people."""
        endpoint = f"{self.base_url}/api/asset/{asset_id}"
        try:
            response = requests.get(endpoint, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"Error getting asset info: {e}")
            return None

    def download_asset(self, asset_id: str) -> Optional[bytes]:
        """Download the original image/file for the asset."""
        # /api/asset/download/{id} or /api/asset/file/{id}?
        # Usually /api/asset/file/{id} or /api/download/asset/{id} in v2?
        # Let's try /api/asset/file/{id} which serves the file.
        # Or /api/asset/download/{id}
        
        # Common Immich endpoints:
        # GET /api/asset/file/:id
        # GET /api/download/asset/:id
        
        # We will try one then the other if needed, but let's start with /api/asset/file/{id}
        # Actually, looking at recent API changes, /api/download/asset/{id} seems more likely for "download".
        # But /api/asset/file/{id} gives the original file often.
        
        endpoint = f"{self.base_url}/api/assets/{asset_id}/original"
        try:
            response = requests.get(endpoint, headers=self.headers, stream=True)
            return response.content
        except requests.RequestException as e:
            print(f"Error downloading asset {asset_id}: {e}")
            return None

    def find_random_group_photo(self, person_ids: List[str]) -> Optional[dict]:
        """
        Finds a random photo containing ALL the specified people.
        """
        # 1. Search (likely returns assets with ANY of the people)
        candidates = self.search_assets(person_ids)
        if not candidates:
            print("No candidates found via search.")
            return None
            
        if not isinstance(candidates, list):
            print(f"Unexpected response type from search: {type(candidates)}")
            return None

        print(f"Found {len(candidates)} candidates. Filtering...")
        
        # Shuffle to get random order
        random.shuffle(candidates)
        
        # 2. Iterate and check details
        # We don't want to spam the API, so we check one by one until we find a match.
        # If the search result itself contains 'people', we can filter immediately.
        
        for candidate in candidates:
            # Check if 'people' key exists and is populated
            asset_people = candidate.get('people', [])
            asset_people_ids = [p['id'] for p in asset_people] if asset_people else []
            
            # If search results are slim, we might need to fetch full details.
            # But usually search results include metadata.
            
            # If the candidate data doesn't have people info, fetch it.
            if 'people' not in candidate:
                 details = self.get_asset_info(candidate['id'])
                 if details:
                     asset_people = details.get('people', [])
                     asset_people_ids = [p['id'] for p in asset_people]
            
            # Check intersection
            # We need ALL person_ids to be present in asset_people_ids
            if all(pid in asset_people_ids for pid in person_ids):
                return candidate
                
        return None

