# Taulu API

## This project needs to do the following:
- Implement the same API functionality as in `main.example.py`
- for the get_image endpoint, the image should be fetched from an Immich v2 server
    - The Immich v2 server located at `https://kuvat.palosuo.fi`.
        - authenticating to the Immich server is to be done with an environment variable called `IMMICH_API_KEY` (from .env file)
    - each photo should contain all of the people described in the `people-ids.json`.
    - after downloading the photo, we need to apply conversions and transformations to it as described in `prepare.example.py`
    - after the photo is converted, it should be served through the endpoint
- The endpoint should serve a new photo each day, and try to serve a different photo each day

## General guidelines:
- The project should be implemented with fully type-hinted python, and be as minimal and easy to deploy as possible.
- use `uv` for managing the python versions and dependencies
- use `git` for version controlling the code

