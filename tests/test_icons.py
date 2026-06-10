from io import BytesIO

import pytest
from rich.console import Console

from barprint.icons import render_icon_bytes


def test_render_icon_bytes_outputs_rich_thumbnail() -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    image = Image.new("RGBA", (4, 4), (220, 40, 20, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    thumbnail = render_icon_bytes(buffer.getvalue(), size=4)
    console = Console(record=True, force_terminal=True, color_system="truecolor", width=20)
    console.print(thumbnail)
    rendered = console.export_text(styles=True)

    assert thumbnail.plain.strip()
    assert "\u2580" in rendered
    assert "\x1b[" in rendered


def test_render_icon_bytes_can_use_ascii_safe_blocks() -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    image = Image.new("RGBA", (2, 2), (20, 80, 220, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    thumbnail = render_icon_bytes(buffer.getvalue(), size=2, use_half_blocks=False)

    assert "\u2580" not in thumbnail.plain
    assert thumbnail.plain == "  \n  "
    assert thumbnail.spans
