#!/usr/bin/env python3
"""Generate Zimi icon assets matching the web UI brand.

Run once to create icon.png, icon.ico, and icon.icns:
    pip install Pillow
    python assets/generate_icons.py

Requires: Pillow. On macOS, uses CoreText for pixel-perfect system font rendering.
On other platforms, falls back to Pillow font rendering.
"""

import os
import platform
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("pip install Pillow", file=sys.stderr)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))

# Brand colors from the web UI CSS:
# background: linear-gradient(135deg, #f59e0b, #f97316, #ef4444);
GRADIENT_COLORS = [
    (245, 158, 11),   # #f59e0b — amber
    (249, 115, 22),   # #f97316 — orange
    (239, 68, 68),    # #ef4444 — red
]
BG_DARK = (10, 10, 11)  # #0a0a0b — matches var(--bg)


def _lerp_color(c1, c2, t):
    """Linear interpolate between two RGB tuples."""
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _gradient_at(x, y, size, colors):
    """Sample a 135-degree linear gradient at (x, y) within a size x size area."""
    t = (x + y) / (2 * size)
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return _lerp_color(colors[0], colors[1], t * 2)
    else:
        return _lerp_color(colors[1], colors[2], (t - 0.5) * 2)


def _render_z_mask_coretext(size, font_size):
    """Render Z glyph using macOS CoreText for pixel-perfect system font match.

    Uses -apple-system Bold (SF Pro Bold at weight 700), the same font
    the web UI renders "Zimi" with in the browser.
    """
    try:
        import CoreText
        import Quartz
        import AppKit

        # Create RGBA bitmap context (CoreText needs explicit foreground color)
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        ctx = Quartz.CGBitmapContextCreate(
            None, size, size, 8, size * 4,
            cs, Quartz.kCGImageAlphaPremultipliedLast
        )

        # Black background
        Quartz.CGContextSetRGBFillColor(ctx, 0, 0, 0, 1.0)
        Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, size, size))

        # Create system font at Bold weight (700) — matches -apple-system bold
        font = CoreText.CTFontCreateWithName(".AppleSystemUIFont", font_size, None)
        bold_font = CoreText.CTFontCreateCopyWithSymbolicTraits(
            font, font_size, None,
            CoreText.kCTFontBoldTrait, CoreText.kCTFontBoldTrait
        )
        if bold_font:
            font = bold_font

        # Create attributed string with white foreground
        white = AppKit.NSColor.whiteColor().CGColor()
        attrs = {
            CoreText.kCTFontAttributeName: font,
            CoreText.kCTForegroundColorAttributeName: white,
        }
        astr = CoreText.CFAttributedStringCreate(None, "Z", attrs)

        # Get glyph size for centering
        line = CoreText.CTLineCreateWithAttributedString(astr)
        bounds = CoreText.CTLineGetBoundsWithOptions(line, 0)
        tx = (size - bounds.size.width) / 2 - bounds.origin.x
        ty = (size - bounds.size.height) / 2 - bounds.origin.y

        # Draw white Z on black background
        Quartz.CGContextSetTextPosition(ctx, tx, ty)
        CoreText.CTLineDraw(line, ctx)

        # Extract RGBA image → convert R channel to grayscale mask
        cg_image = Quartz.CGBitmapContextCreateImage(ctx)
        data_provider = Quartz.CGImageGetDataProvider(cg_image)
        data = Quartz.CGDataProviderCopyData(data_provider)

        img = Image.frombytes("RGBA", (size, size), bytes(data))
        mask = img.split()[0]  # R channel = white text intensity
        print("  Using CoreText system font (SF Pro Bold)")
        return mask

    except Exception as e:
        print(f"  CoreText failed ({e}), falling back to Pillow")
        return None


def _render_z_mask_pillow(size, font_size):
    """Fallback: render Z glyph using Pillow font rendering."""
    font = None
    fonts = [
        ("/System/Library/Fonts/SFCompact.ttf", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 1),
        ("C:\\Windows\\Fonts\\segoeuib.ttf", 0),
        ("C:\\Windows\\Fonts\\arialbd.ttf", 0),
    ]
    for font_path, font_index in fonts:
        try:
            font = ImageFont.truetype(font_path, font_size, index=font_index)
            name = font.getname()
            print(f"  Using Pillow font: {name[0]} {name[1]}")
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()
        print("  Using Pillow default font")

    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    bbox = mask_draw.textbbox((0, 0), "Z", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1]
    mask_draw.text((tx, ty), "Z", fill=255, font=font)
    return mask


def create_icon(size=1024):
    """Create Zimi icon — gradient Z on dark rounded rectangle with macOS-style padding."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # macOS icon padding: ~18% on each side → content is ~64% of canvas
    padding = int(size * 0.1)
    box_size = size - 2 * padding
    corner_radius = int(box_size * 0.223)  # Apple squircle ratio

    # Dark rounded rectangle background
    draw.rounded_rectangle(
        [padding, padding, padding + box_size - 1, padding + box_size - 1],
        radius=corner_radius,
        fill=BG_DARK,
    )

    # Subtle border
    draw.rounded_rectangle(
        [padding, padding, padding + box_size - 1, padding + box_size - 1],
        radius=corner_radius,
        outline=(39, 39, 43, 180),
        width=max(1, size // 200),
    )

    # Render Z glyph as mask — try CoreText first (macOS), fall back to Pillow
    font_size = int(box_size * 0.72)
    mask = None
    if platform.system() == "Darwin":
        mask = _render_z_mask_coretext(size, font_size)
    if mask is None:
        mask = _render_z_mask_pillow(size, font_size)

    # Apply gradient to Z mask
    gradient = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad_pixels = gradient.load()
    for y in range(size):
        for x in range(size):
            alpha = mask.getpixel((x, y))
            if alpha > 0:
                color = _gradient_at(x, y, size, GRADIENT_COLORS)
                grad_pixels[x, y] = (*color, alpha)

    # Composite gradient Z onto the icon
    img = Image.alpha_composite(img, gradient)
    return img


def save_icns_with_iconutil(icon, icns_path):
    """Use macOS iconutil for proper .icns with all required sizes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        iconset_dir = os.path.join(tmpdir, "icon.iconset")
        os.makedirs(iconset_dir)

        icon_sizes = [
            ("icon_16x16.png", 16),
            ("icon_16x16@2x.png", 32),
            ("icon_32x32.png", 32),
            ("icon_32x32@2x.png", 64),
            ("icon_128x128.png", 128),
            ("icon_128x128@2x.png", 256),
            ("icon_256x256.png", 256),
            ("icon_256x256@2x.png", 512),
            ("icon_512x512.png", 512),
            ("icon_512x512@2x.png", 1024),
        ]
        for filename, sz in icon_sizes:
            resized = icon.resize((sz, sz), Image.LANCZOS)
            resized.save(os.path.join(iconset_dir, filename))

        subprocess.run(
            ["iconutil", "-c", "icns", iconset_dir, "-o", icns_path],
            check=True,
        )


def main():
    print("Generating Zimi icons...")
    icon = create_icon(1024)

    # PNG (256px for general use)
    png_path = os.path.join(HERE, "icon.png")
    icon_256 = icon.resize((256, 256), Image.LANCZOS)
    icon_256.save(png_path)
    print(f"  Created {png_path}")

    # Favicon (32x32 PNG for browser tabs)
    favicon_path = os.path.join(HERE, "favicon.png")
    icon_32 = icon.resize((32, 32), Image.LANCZOS)
    icon_32.save(favicon_path)
    print(f"  Created {favicon_path}")

    # ICO (Windows) — multi-size
    ico_path = os.path.join(HERE, "icon.ico")
    sizes = [16, 32, 48, 64, 128, 256]
    ico_images = [icon.resize((s, s), Image.LANCZOS) for s in sizes]
    ico_images[0].save(ico_path, format="ICO", append_images=ico_images[1:],
                       sizes=[(s, s) for s in sizes])
    print(f"  Created {ico_path}")

    # ICNS (macOS)
    icns_path = os.path.join(HERE, "icon.icns")
    try:
        save_icns_with_iconutil(icon, icns_path)
        print(f"  Created {icns_path}")
    except (FileNotFoundError, subprocess.CalledProcessError):
        try:
            icon.save(icns_path, format="ICNS")
            print(f"  Created {icns_path} (via Pillow)")
        except Exception as e:
            print(f"  Skipped .icns: {e}")

    print("Done!")


if __name__ == "__main__":
    main()
