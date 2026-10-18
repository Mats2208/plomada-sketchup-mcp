"""A PNG of a plan or a drawing without SketchUp: ezdxf's drawing add-on on its PyMuPDF backend.

Grey scale on white, as plotted: entities on layers that do not plot (the data
layers of write_plan's drawing style) or are off or frozen are left out. The
image is shrunk until it is under MAX_BYTES, like capture_view.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ezdxf import bbox
from ezdxf.document import Drawing

MAX_BYTES = 350_000  # largest PNG returned
PAGE_MM = 400.0  # page width the drawing is fitted into
MARGIN_MM = 8.0
MIN_PX = 320  # the smallest image tried before giving up on MAX_BYTES
HATCH_LINES = 1500  # pattern lines closer than the drawing's size over this are drawn as a grey fill


class PreviewError(RuntimeError):
    """The preview cannot be drawn; the message says why."""


def _plots(doc: Drawing, entity: Any) -> bool:
    layer = doc.layers.get(entity.dxf.layer) if doc.layers.has_entry(entity.dxf.layer) else None
    return layer is None or layer.dxf.get("plot", 1) != 0


def render(doc: Drawing, width_px: int = 1600) -> tuple[bytes, int, int]:
    """(PNG bytes, width px, height px) of the document's model space, fitted into width_px."""
    try:
        from ezdxf.addons.drawing import Frontend, RenderContext, layout, pymupdf
        from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    except ImportError as exc:  # pragma: no cover - both are dependencies of the bridge
        raise PreviewError(f"the preview needs PyMuPDF and Pillow ({exc.name} is missing): run uv sync") from None
    msp = doc.modelspace()
    shown = [e for e in msp if _plots(doc, e)]
    box = bbox.extents(shown, fast=True)
    if not box.has_data or (box.size.x <= 0 and box.size.y <= 0):
        raise PreviewError("the drawing has nothing to plot")
    w, h = max(box.size.x, 1e-6), max(box.size.y, 1e-6)
    page_h = min(max(PAGE_MM * h / w, 40.0), 3.0 * PAGE_MM)
    page = layout.Page(PAGE_MM, page_h, layout.Units.mm, margins=layout.Margins.all(MARGIN_MM))
    backend = pymupdf.PyMuPdfBackend()
    config = Configuration(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=ColorPolicy.MONOCHROME_LIGHT_BG,
        min_hatch_line_distance=max(w, h) / HATCH_LINES,
    )
    Frontend(RenderContext(doc), backend, config=config).draw_layout(msp, filter_func=lambda e: _plots(doc, e))
    px = width_px
    while True:
        dpi = max(round(px / (PAGE_MM / 25.4)), 1)
        png = backend.get_pixmap_bytes(page, fmt="png", dpi=dpi)
        if not png:
            raise PreviewError("PyMuPDF could not draw the page")
        if len(png) <= MAX_BYTES or px <= MIN_PX:
            return png, round(PAGE_MM / 25.4 * dpi), round(page_h / 25.4 * dpi)
        px = int(px * 0.75)


def save(png: bytes, stem: str) -> Path:
    """Writes the PNG to %TEMP% as plomada_preview_<stem>.png; returns its path."""
    path = Path(tempfile.gettempdir()) / f"plomada_preview_{stem}.png"
    path.write_bytes(png)
    return path
