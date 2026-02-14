import os
from PIL import Image
import numpy as np
from io import BytesIO

# 6-color palette for GDEP073E01 Spectra E6 display
# Black, White, Yellow, Red, Blue, Green

# Theoretical palette - used for output (firmware compatibility)
PALETTE_THEORETICAL = [
    (0, 0, 0),       # Black
    (255, 255, 255), # White
    (255, 255, 0),   # Yellow
    (255, 0, 0),     # Red
    (0, 0, 255),     # Blue
    (0, 255, 0),     # Green
]

# Measured palette - actual colors displayed by e-paper (used for dithering)
# These values are measured from the actual e-paper display and represent
# what colors are actually shown, not what the theoretical values suggest.
# This dramatically improves image quality by making dithering decisions
# based on what will actually be displayed.
PALETTE_MEASURED = [
    (2, 2, 2),         # Black - nearly perfect
    (190, 200, 200),   # White - actually light gray! (30% darker than theoretical)
    (205, 202, 0),     # Yellow - darker than expected
    (135, 19, 0),      # Red - much darker (54% reduction)
    (5, 64, 158),      # Blue - much darker (58% reduction)
    (39, 102, 60),     # Green - extremely dark (73-87% reduction)
]

# Keep backward compatibility
PALETTE_COLORS = PALETTE_THEORETICAL


# Gamma correction lookup tables for sRGB <-> linear conversion
def srgb_to_linear(srgb_value: int) -> float:
    """Convert sRGB byte value (0-255) to linear light (0.0-1.0)"""
    s = srgb_value / 255.0
    if s <= 0.04045:
        return s / 12.92
    return ((s + 0.055) / 1.055) ** 2.4


def linear_to_srgb(linear_value: float) -> int:
    """Convert linear light (0.0-1.0) to sRGB byte value (0-255)"""
    if linear_value <= 0.0:
        return 0
    if linear_value >= 1.0:
        return 255
    if linear_value <= 0.0031308:
        s = 12.92 * linear_value
    else:
        s = 1.055 * (linear_value ** (1.0 / 2.4)) - 0.055
    return int(np.clip(s * 255.0 + 0.5, 0, 255))


def compress_dynamic_range(image_array: np.ndarray, measured_palette: list) -> np.ndarray:
    """
    Compress the image's dynamic range to fit e-paper's limited range.

    E-paper displays have much darker whites and limited dynamic range compared
    to modern displays. This function maps the full 0-255 range to the actual
    range the display can show (e.g., 2-200 instead of 0-255).

    Args:
        image_array: RGB image as numpy array (H, W, 3)
        measured_palette: List of measured RGB tuples

    Returns:
        Compressed image array
    """
    # Calculate luminance of display's black and white in linear space
    # Using Rec. 709 coefficients: Y = 0.2126*R + 0.7152*G + 0.0722*B
    black_rgb = measured_palette[0]
    white_rgb = measured_palette[1]

    black_Y = (0.2126729 * srgb_to_linear(black_rgb[0]) +
               0.7151522 * srgb_to_linear(black_rgb[1]) +
               0.0721750 * srgb_to_linear(black_rgb[2]))

    white_Y = (0.2126729 * srgb_to_linear(white_rgb[0]) +
               0.7151522 * srgb_to_linear(white_rgb[1]) +
               0.0721750 * srgb_to_linear(white_rgb[2]))

    display_range = white_Y - black_Y

    # Process each pixel
    result = np.zeros_like(image_array, dtype=np.uint8)
    height, width = image_array.shape[:2]

    for y in range(height):
        for x in range(width):
            r, g, b = image_array[y, x]

            # Convert to linear
            lr = srgb_to_linear(r)
            lg = srgb_to_linear(g)
            lb = srgb_to_linear(b)

            # Original luminance
            Y = 0.2126729 * lr + 0.7151522 * lg + 0.0721750 * lb

            # Compress to display range
            compressed_Y = black_Y + Y * display_range

            # Scale RGB proportionally
            if Y > 1e-6:
                scale = compressed_Y / Y
                lr *= scale
                lg *= scale
                lb *= scale
            else:
                # Near-black pixel
                lr = lg = lb = black_Y

            # Convert back to sRGB
            result[y, x] = [linear_to_srgb(lr), linear_to_srgb(lg), linear_to_srgb(lb)]

    return result


def find_closest_palette_index(r: int, g: int, b: int, palette: list) -> int:
    """Find the closest color in the palette using Euclidean distance"""
    min_dist = float('inf')
    closest_idx = 1  # Default to white

    for i, (pr, pg, pb) in enumerate(palette):
        dr = r - pr
        dg = g - pg
        db = b - pb
        dist = dr*dr + dg*dg + db*db

        if dist < min_dist:
            min_dist = dist
            closest_idx = i

    return closest_idx


def floyd_steinberg_dither(image_array: np.ndarray,
                          measured_palette: list,
                          theoretical_palette: list) -> np.ndarray:
    """
    Apply Floyd-Steinberg error diffusion dithering.

    Uses measured palette for dithering decisions (what will actually display)
    but outputs theoretical palette colors (for firmware compatibility).

    Args:
        image_array: RGB image as numpy array (H, W, 3)
        measured_palette: Measured colors for dithering decisions
        theoretical_palette: Theoretical colors for output

    Returns:
        Dithered image with theoretical palette colors
    """
    height, width = image_array.shape[:2]

    # Working buffer with error accumulation (float for precision)
    working = image_array.astype(np.float32)

    # Output buffer
    result = np.zeros((height, width, 3), dtype=np.uint8)

    # Floyd-Steinberg diffusion matrix:
    # [ * 7 ]   (current pixel gets error, then distribute)
    # [ 3 5 1 ]
    # All values / 16

    for y in range(height):
        for x in range(width):
            # Get current pixel with accumulated error
            old_pixel = working[y, x]
            old_r = int(np.clip(old_pixel[0], 0, 255))
            old_g = int(np.clip(old_pixel[1], 0, 255))
            old_b = int(np.clip(old_pixel[2], 0, 255))

            # Find closest color using MEASURED palette
            closest_idx = find_closest_palette_index(old_r, old_g, old_b, measured_palette)

            # Output using THEORETICAL palette (firmware compatibility)
            result[y, x] = theoretical_palette[closest_idx]

            # Calculate error using MEASURED palette (accurate error diffusion)
            measured_color = measured_palette[closest_idx]
            err_r = old_r - measured_color[0]
            err_g = old_g - measured_color[1]
            err_b = old_b - measured_color[2]

            # Distribute error to neighboring pixels
            if x + 1 < width:
                working[y, x + 1] += np.array([err_r, err_g, err_b]) * 7.0 / 16.0

            if y + 1 < height:
                if x > 0:
                    working[y + 1, x - 1] += np.array([err_r, err_g, err_b]) * 3.0 / 16.0

                working[y + 1, x] += np.array([err_r, err_g, err_b]) * 5.0 / 16.0

                if x + 1 < width:
                    working[y + 1, x + 1] += np.array([err_r, err_g, err_b]) * 1.0 / 16.0

    return result


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


def convert_image_to_bin(image_input: str | BytesIO, use_optimizations: bool = True) -> bytes:
    """
    Reads an image, processes it for the Spectra E6 display, and returns the binary data.

    Processing steps:
    1. Resize/Crop to 1600x1200
    2. Rotate 90 degrees (becoming 1200x1600)
    3. Apply dynamic range compression (if optimizations enabled)
    4. Apply Floyd-Steinberg dithering with measured palette (if optimizations enabled)
    5. Pack pixels (4 bits per pixel)

    Args:
        image_input: Path to image file or BytesIO object
        use_optimizations: If True, use measured palette and dithering (default: True)
                          If False, use legacy quantization method
    """
    if isinstance(image_input, (str, os.PathLike)):
        image = Image.open(image_input)
    else:
        image = Image.open(image_input)

    # Resize to 1600x1200 (before rotation)
    image = resize_and_truncate(image, (1600, 1200))

    # Rotate 90 degrees
    image = image.rotate(90, expand=True)

    # Convert to RGB
    image = image.convert('RGB')

    if use_optimizations:
        # NEW OPTIMIZED PATH: Measured palette + dithering
        # Convert to numpy array for processing
        image_array = np.array(image, dtype=np.uint8)

        # Step 1: Compress dynamic range to fit e-paper's limited range
        print("Compressing dynamic range for e-paper...")
        image_array = compress_dynamic_range(image_array, PALETTE_MEASURED)

        # Step 2: Apply Floyd-Steinberg dithering with measured palette
        print("Applying Floyd-Steinberg dithering with measured palette...")
        image_array = floyd_steinberg_dither(image_array, PALETTE_MEASURED, PALETTE_THEORETICAL)

        # Step 3: Map RGB colors to palette indices
        # Since dithering already outputs theoretical palette colors,
        # we can do exact matching
        height, width = image_array.shape[:2]
        image_data = np.zeros((height, width), dtype=np.uint8)

        for y in range(height):
            for x in range(width):
                r, g, b = image_array[y, x]
                # Find exact match in theoretical palette
                for idx, (pr, pg, pb) in enumerate(PALETTE_THEORETICAL):
                    if r == pr and g == pg and b == pb:
                        image_data[y, x] = idx
                        break
    else:
        # LEGACY PATH: Simple quantization
        # Create palette image for quantization
        palette_img = Image.new('P', (1, 1))
        palette_flat = [col for p in PALETTE_COLORS for col in p] + [0] * (256 - len(PALETTE_COLORS)) * 3
        palette_img.putpalette(palette_flat)

        # Quantize to palette
        image = image.quantize(palette=palette_img)
        image_data = np.array(image)

    # Mapping fix: "Number 4 is not used in Spectra, blue and green are 5 and 6"
    # Palette indices: 0:Black, 1:White, 2:Yellow, 3:Red, 4:Blue, 5:Green
    # If index < 4 (0,1,2,3), keep it.
    # If index >= 4 (4,5), add 1 -> (5,6).
    # So index 4 (Blue) becomes 5. Index 5 (Green) becomes 6.
    # Resulting indices: 0, 1, 2, 3, 5, 6 (index 4 reserved/unused).
    image_data = np.where(image_data < 4, image_data, image_data + 1)

    # Pack 2 pixels per byte (4 bits per pixel)
    # Even column (pixel 0) is high nibble (*16), odd column (pixel 1) is low nibble
    colors = image_data[:, 1::2] + 16 * image_data[:, ::2]

    return colors.tobytes()
