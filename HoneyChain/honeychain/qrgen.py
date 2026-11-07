"""
honeychain.qrgen
================
Printable PNG QR codes pointing at the public verification page.

Backends, tried in order:

1. ``qrcode`` + ``Pillow`` -- the declared dependency in ``requirements.txt``
   and the backend used in a normal install.
2. ``reportlab.graphics.barcode.qrencoder`` + ``Pillow`` -- a fallback that
   ships with ReportLab. It produces the module matrix, which is rendered to a
   PNG here. This exists so QR generation still works in a constrained /
   air-gapped environment where ``qrcode`` was not installable.

Both backends emit a standard QR symbol; the fallback is verified against an
independent decoder in ``tests/test_qr.py``.

Filenames are derived from the batch id through a strict allowlist, so a batch
id can never escape ``static/qrcodes/`` via ``..`` or an absolute path.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

#: Only these characters survive into a filename.
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_\-]")

#: Pixels per QR module, and quiet-zone width in modules (the spec requires 4).
MODULE_PIXELS = 10
QUIET_ZONE_MODULES = 4


class QRGenerationError(Exception):
    """Raised when no QR backend could produce an image."""


def safe_filename(batch_id: str) -> str:
    """Map a batch id onto a filename that cannot traverse out of the QR folder."""
    cleaned = _SAFE_FILENAME.sub("_", str(batch_id)).strip("._-")
    if not cleaned:
        raise QRGenerationError("Batch id contains no characters usable in a filename")
    return f"{cleaned[:96]}.png"


def _render_with_qrcode(data: str, destination: Path) -> bool:
    try:
        import qrcode  # type: ignore
        from qrcode.constants import ERROR_CORRECT_M  # type: ignore
    except ImportError:
        return False

    try:
        engine = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=MODULE_PIXELS,
            border=QUIET_ZONE_MODULES,
        )
        engine.add_data(data)
        engine.make(fit=True)
        image = engine.make_image(fill_color="black", back_color="white")
        image.save(destination)
        return True
    except Exception as exc:  # pragma: no cover - backend-specific failure
        logger.warning("qrcode backend failed (%s); trying fallback", type(exc).__name__)
        return False


def _render_with_reportlab(data: str, destination: Path) -> bool:
    """Fallback: build the module matrix with ReportLab, rasterise with Pillow."""
    try:
        from PIL import Image
        from reportlab.graphics.barcode.qrencoder import QRCode, QRErrorCorrectLevel  # type: ignore
    except ImportError:
        return False

    try:
        encoder = QRCode(None, QRErrorCorrectLevel.M)  # None => auto-select version
        encoder.addData(data)
        encoder.make()
        matrix = encoder.modules
        if not matrix:
            return False

        modules = len(matrix)
        side = (modules + 2 * QUIET_ZONE_MODULES) * MODULE_PIXELS
        image = Image.new("L", (side, side), 255)
        pixels = image.load()

        for row in range(modules):
            for column in range(modules):
                if not matrix[row][column]:
                    continue
                top = (QUIET_ZONE_MODULES + row) * MODULE_PIXELS
                left = (QUIET_ZONE_MODULES + column) * MODULE_PIXELS
                for y in range(top, top + MODULE_PIXELS):
                    for x in range(left, left + MODULE_PIXELS):
                        pixels[x, y] = 0

        image.save(destination, format="PNG")
        return True
    except Exception as exc:  # pragma: no cover - backend-specific failure
        logger.warning("ReportLab QR fallback failed (%s)", type(exc).__name__)
        return False


#: Hard ceilings on the render parameters an HTTP caller may influence.
#: A version-40 symbol is 177 modules wide; at box_size=40 with a border that
#: is a 7480x7480 image, ~56M pixels. Without a cap, a single query string
#: could make the server allocate hundreds of megabytes. These bounds keep the
#: worst case near 2 MB.
MAX_BOX_SIZE = 20
MAX_BORDER = 16

#: Hard floor on the quiet zone. The QR specification mandates 4 modules; a
#: symbol rendered with border=0 is genuinely undecodable (verified against an
#: independent decoder), so 0 is silently raised to 1 rather than handed back
#: as a broken image. Values below 4 depart from the spec and may fail on
#: cheap scanners or in poor lighting -- only go below 4 when the surrounding
#: layout supplies its own white margin.
MIN_BORDER = 1
#: Byte-mode capacity of a version-40 symbol at error correction level L.
#: Anything longer cannot be encoded at all, so reject it before rendering.
MAX_PAYLOAD_BYTES = 2953

#: Error-correction levels, lowest to highest redundancy.
ERROR_CORRECTION_LEVELS = ("L", "M", "Q", "H")


def render_qr_png_bytes(
    data: str,
    box_size: int = MODULE_PIXELS,
    border: int = QUIET_ZONE_MODULES,
    error_correction: str = "M",
) -> bytes:
    """Render a QR code to PNG bytes in memory, without touching the filesystem.

    Used by the HTTP layer to stream a QR straight to the client. Nothing is
    written to disk, so concurrent requests cannot collide on a filename and no
    cleanup is needed.

    Parameters are clamped rather than rejected, because a caller asking for an
    oversized image wants a big image, not an error -- but they do not get to
    choose how much memory the server spends. ``data`` that cannot be encoded
    at all does raise, since silently truncating a payload would produce a QR
    that scans to the wrong thing.

    Raises
    ------
    QRGenerationError
        If ``data`` is empty, exceeds QR capacity, or no backend is available.
    """
    if not data:
        raise QRGenerationError("Cannot encode an empty payload")

    encoded_length = len(data.encode("utf-8"))
    if encoded_length > MAX_PAYLOAD_BYTES:
        raise QRGenerationError(
            f"Payload is {encoded_length} bytes; the maximum a QR symbol can "
            f"hold is {MAX_PAYLOAD_BYTES}"
        )

    box_size = max(1, min(int(box_size), MAX_BOX_SIZE))
    border = max(MIN_BORDER, min(int(border), MAX_BORDER))
    level = str(error_correction).upper()
    if level not in ERROR_CORRECTION_LEVELS:
        level = "M"

    buffer = io.BytesIO()

    # -- preferred backend: the `qrcode` library --------------------------
    try:
        import qrcode  # type: ignore
        from qrcode import constants  # type: ignore

        engine = qrcode.QRCode(
            version=None,  # auto-select the smallest symbol that fits
            error_correction=getattr(constants, f"ERROR_CORRECT_{level}"),
            box_size=box_size,
            border=border,
        )
        engine.add_data(data)
        engine.make(fit=True)
        engine.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
        buffer.seek(0)
        return buffer.getvalue()
    except ImportError:
        pass  # fall through to the bundled backend
    except Exception as exc:  # pragma: no cover - backend-specific failure
        logger.warning("qrcode backend failed (%s); trying fallback", type(exc).__name__)
        buffer = io.BytesIO()

    # -- fallback backend: ReportLab's encoder, rasterised with Pillow ----
    try:
        from PIL import Image
        from reportlab.graphics.barcode.qrencoder import (  # type: ignore
            QRCode,
            QRErrorCorrectLevel,
        )
    except ImportError:
        raise QRGenerationError(
            "No QR backend available. Install the declared dependencies with "
            "`pip install qrcode Pillow`."
        ) from None

    try:
        encoder = QRCode(None, getattr(QRErrorCorrectLevel, level))
        encoder.addData(data)
        encoder.make()
        matrix = encoder.modules
        if not matrix:
            raise QRGenerationError("QR encoder produced an empty symbol")

        modules = len(matrix)
        side = (modules + 2 * border) * box_size
        image = Image.new("L", (side, side), 255)
        pixels = image.load()

        for row in range(modules):
            for column in range(modules):
                if not matrix[row][column]:
                    continue
                top = (border + row) * box_size
                left = (border + column) * box_size
                for y in range(top, top + box_size):
                    for x in range(left, left + box_size):
                        pixels[x, y] = 0

        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer.getvalue()
    except QRGenerationError:
        raise
    except Exception as exc:
        raise QRGenerationError(
            f"QR rendering failed: {type(exc).__name__}"
        ) from exc


def generate_batch_qr(batch_id: str, overwrite: bool = True) -> dict[str, Any]:
    """Write ``static/qrcodes/<batch>.png`` encoding the batch verification URL.

    Returns the encoded URL, the static-relative path and the absolute path.
    """
    config.ensure_runtime_directories()

    filename = safe_filename(batch_id)
    destination = (config.QRCODE_DIR / filename).resolve()

    # Belt-and-braces containment check on top of the filename allowlist.
    if config.QRCODE_DIR.resolve() not in destination.parents:
        raise QRGenerationError("Refusing to write a QR code outside static/qrcodes")

    verify_url = config.public_verify_url(batch_id)

    if destination.exists() and not overwrite:
        return {
            "url": verify_url,
            "filename": filename,
            "static_path": f"qrcodes/{filename}",
            "absolute_path": str(destination),
            "backend": "cached",
        }

    for backend_name, render in (
        ("qrcode", _render_with_qrcode),
        ("reportlab", _render_with_reportlab),
    ):
        if render(verify_url, destination):
            logger.info("QR generated for %s via %s backend", batch_id, backend_name)
            return {
                "url": verify_url,
                "filename": filename,
                "static_path": f"qrcodes/{filename}",
                "absolute_path": str(destination),
                "backend": backend_name,
            }

    raise QRGenerationError(
        "No QR backend available. Install the declared dependencies with "
        "`pip install qrcode Pillow`."
    )
