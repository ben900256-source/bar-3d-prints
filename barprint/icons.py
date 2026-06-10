from __future__ import annotations

from io import BytesIO

from rich.text import Text

from .bar_assets import UnitAsset, read_unit_icon_bytes


class IconRenderError(ValueError):
    pass


def render_unit_icon(unit: UnitAsset, size: int = 16, *, use_half_blocks: bool = True) -> Text:
    image_bytes = read_unit_icon_bytes(unit)
    if not image_bytes:
        return Text("")
    return render_icon_bytes(image_bytes, size=size, use_half_blocks=use_half_blocks)


def render_icon_bytes(image_bytes: bytes, size: int = 16, *, use_half_blocks: bool = True) -> Text:
    if size < 1:
        raise IconRenderError("Icon size must be at least 1.")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ModuleNotFoundError as exc:
        raise IconRenderError("Pillow is required to render unit icons.") from exc

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image = image.convert("RGBA")
            image = ImageOps.contain(image, (size, size), method=Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            offset = ((size - image.width) // 2, (size - image.height) // 2)
            canvas.alpha_composite(image, offset)
            pixels = canvas.load()
            if use_half_blocks:
                return _render_rgba_pixels_half_blocks(pixels, size)
            return _render_rgba_pixels_full_blocks(pixels, size)
    except (OSError, UnidentifiedImageError) as exc:
        raise IconRenderError(f"Could not decode unit icon: {exc}") from exc


def _render_rgba_pixels_half_blocks(pixels, size: int) -> Text:
    rendered = Text()
    for y in range(0, size, 2):
        if y:
            rendered.append("\n")
        for x in range(size):
            top = _blend_rgba(pixels[x, y])
            bottom = _blend_rgba(pixels[x, y + 1]) if y + 1 < size else (0, 0, 0)
            if top == (0, 0, 0) and bottom == (0, 0, 0):
                rendered.append(" ")
            else:
                rendered.append("\u2580", style=f"{_hex_color(top)} on {_hex_color(bottom)}")
    return rendered


def _render_rgba_pixels_full_blocks(pixels, size: int) -> Text:
    rendered = Text()
    for y in range(size):
        if y:
            rendered.append("\n")
        for x in range(size):
            color = _blend_rgba(pixels[x, y])
            rendered.append(" ", style=f"on {_hex_color(color)}")
    return rendered


def _blend_rgba(rgba: tuple[int, int, int, int]) -> tuple[int, int, int]:
    red, green, blue, alpha = rgba
    opacity = alpha / 255
    return (
        round(red * opacity),
        round(green * opacity),
        round(blue * opacity),
    )


def _hex_color(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
