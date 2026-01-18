import os
from PIL import Image
import numpy as np
from io import BytesIO

# 6-color palette for GDEP073E01 Spectra E6 display
# Black, White, Yellow, Red, Blue, Green
PALETTE_COLORS = [
    (0, 0, 0),       # Black
    (255, 255, 255), # White
    (255, 255, 0),   # Yellow
    (255, 0, 0),     # Red
    (0, 0, 255),     # Blue
    (0, 255, 0),     # Green
]

def resize_and_truncate(image: Image.Image, target_size: tuple[int, int] = (1600, 1200)) -> Image.Image:
    """Resize the image to fit the target size and truncate symmetrically"""
    # Calculate the aspect ratio
    aspect_ratio = image.width / image.height
    target_aspect_ratio = target_size[0] / target_size[1]

    # Resize the image proportionally
    if aspect_ratio > target_aspect_ratio:
        # Resize by height and then truncate the width symmetrically
        new_width = int(target_size[1] * aspect_ratio)
        resized_image = image.resize((new_width, target_size[1]), Image.LANCZOS) # type: ignore
        left_margin = (resized_image.width - target_size[0]) // 2
        right_margin = (resized_image.width - target_size[0] + 1) // 2
        top_margin = 0
        bottom_margin = 0
        
        # Crop
        truncated_image = resized_image.crop((
            left_margin, 
            top_margin, 
            resized_image.width - right_margin, 
            resized_image.height - bottom_margin
        ))

    else:
        # Resize by width and then truncate the height symmetrically
        new_height = int(target_size[0] / aspect_ratio)
        resized_image = image.resize((target_size[0], new_height), Image.LANCZOS) # type: ignore
        left_margin = 0
        right_margin = 0
        top_margin = (resized_image.height - target_size[1]) // 2
        bottom_margin = (resized_image.height - target_size[1] + 1) // 2

        # Crop
        truncated_image = resized_image.crop((
            left_margin, 
            top_margin, 
            resized_image.width - right_margin, 
            resized_image.height - bottom_margin
        ))

    return truncated_image


def convert_image_to_bin(image_input: str | BytesIO) -> bytes:
    """
    Reads an image, processes it for the Spectra E6 display, and returns the binary data.
    
    Processing steps:
    1. Resize/Crop to 1600x1200
    2. Rotate 90 degrees (becoming 1200x1600)
    3. Quantize to 6-color palette
    4. Pack pixels (4 bits per pixel)
    """
    if isinstance(image_input, (str, os.PathLike)):
        image = Image.open(image_input)
    else:
        image = Image.open(image_input)

    # Resize to 1600x1200 (before rotation)
    image = resize_and_truncate(image, (1600, 1200))
    
    # Rotate 90 degrees
    image = image.rotate(90, expand=True)
    
    # Create palette image for quantization
    palette_img = Image.new('P', (1, 1))
    palette_flat = [col for p in PALETTE_COLORS for col in p] + [0] * (256 - len(PALETTE_COLORS)) * 3
    palette_img.putpalette(palette_flat)
    
    # Convert to RGB then Quantize
    # Note: Example used convert('RGB') before quantize.
    image = image.convert('RGB').quantize(palette=palette_img)
    
    image_data = np.array(image)
    
    # Mapping fix from example:
    # "Number 4 is not used in Spectra, blue and green are 5 and 6"
    # Palette indices: 0:Black, 1:White, 2:Yellow, 3:Red, 4:Blue, 5:Green in the list above?
    # Wait, let's check the example's palette vs the remapping logic.
    # Example palette:
    # 0: (0,0,0) Black
    # 1: (255,255,255) White
    # 2: (255,255,0) Yellow
    # 3: (255,0,0) Red
    # 4: (0,0,255) Blue
    # 5: (0,255,0) Green
    
    # Example logic: `image_data = np.where(image_data < 4, image_data, image_data + 1)`
    # If index < 4 (0,1,2,3), keep it.
    # If index >= 4 (4,5), add 1 -> (5,6).
    # So index 4 (Blue) becomes 5. Index 5 (Green) becomes 6.
    # Resulting indices: 0, 1, 2, 3, 5, 6.
    # This matches the comment "Number 4 is not used".
    
    image_data = np.where(image_data < 4, image_data, image_data + 1)
    
    # Pack 2 pixels per byte
    # 4 bits per pixel.
    # The example does: `colors = image_data[:, 1::2] + 16 * image_data[:, ::2]`
    # This means the even column (pixel 0) is the high nibble (x16), and odd column (pixel 1) is low nibble.
    # pixel 0 is at ::2, pixel 1 is at 1::2.
    
    # Ensure types are capable of holding the sum (uint8 is fine since max is 6*16 + 6 = 102, well within 255)
    colors = image_data[:, 1::2] + 16 * image_data[:, ::2]
    
    return colors.tobytes()
