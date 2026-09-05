"""The document the Canvas is editing, and enough history to undo the big steps.

Strokes are undone by the canvas itself. What this holds is the structural
work - receive, open, apply crop, expand, clear, invert, reset - because
those replace the canvas's contents wholesale.
"""

from __future__ import annotations

import typing

from PIL import Image

from . import imaging

HISTORY_LIMIT = 6


class Document:
    """Working image, mask coverage, and where they came from."""

    def __init__(self) -> None:
        self.image: typing.Optional[Image.Image] = None
        self.mask: typing.Optional[Image.Image] = None
        self.original: typing.Optional[Image.Image] = None
        self.origin: str = "none"
        self.filename: typing.Optional[str] = None
        self.has_expansion: bool = False
        self.expansion: dict = {}
        self.history: typing.List[dict] = []
        self.future: typing.List[dict] = []
        self.last_send: str = ""
        self._original_png: typing.Optional[bytes] = None
        self._original_ref: typing.Optional[Image.Image] = None
        # A send into Inpaint writes the mask in a later step; this is what
        # that step needs to know, and it is consumed by it.
        self.pending_send: typing.Optional[dict] = None

    # -- state -------------------------------------------------------------

    @property
    def size(self) -> typing.Optional[typing.Tuple[int, int]]:
        return self.image.size if self.image is not None else None

    @property
    def has_image(self) -> bool:
        return self.image is not None

    @property
    def has_mask(self) -> bool:
        return not imaging.mask_is_empty(self.mask)

    def original_size_text(self) -> str:
        source = self.original if self.original is not None else self.image
        return f"{source.width}x{source.height}" if source is not None else ""

    def describe(self) -> str:
        if self.image is None:
            return "No image"
        width, height = self.image.size
        parts = [f"{width} × {height}"]
        if self.has_expansion:
            parts.append("expanded")
        if self.has_mask:
            parts.append("mask")
        if self.filename:
            parts.append(self.filename)
        elif self.origin not in ("none", "file"):
            parts.append(f"from {self.origin}")
        return " · ".join(parts)

    # -- history -----------------------------------------------------------

    def _original_bytes(self) -> typing.Optional[bytes]:
        """The original as PNG, encoded once and shared by every snapshot
        taken while it is the original."""
        if self.original is None:
            return None
        if self._original_png is None or self._original_ref is not self.original:
            self._original_png = imaging.to_png_bytes(self.original)
            self._original_ref = self.original
        return self._original_png

    def _snapshot(self, label: str) -> dict:
        """A whole document, small enough to keep a handful of.

        PNG rather than the images themselves: a structural step is rare and
        slow anyway, and six uncompressed 4K RGBA frames is a lot of server
        memory per open tab.
        """
        return {
            "label": label,
            "image": imaging.to_png_bytes(self.image) if self.image is not None else None,
            "mask": imaging.to_png_bytes(self.mask) if self.has_mask else None,
            "original": self._original_bytes(),
            "origin": self.origin,
            "filename": self.filename,
            "has_expansion": self.has_expansion,
            "expansion": dict(self.expansion),
        }

    def _restore(self, snapshot: dict) -> None:
        image = snapshot.get("image")
        mask = snapshot.get("mask")
        original = snapshot.get("original")
        self.image = imaging.from_png_bytes(image) if image else None
        self.mask = imaging.from_png_bytes(mask).convert("L") if mask else None
        if original is None:
            self.original = None
        elif original is not self._original_png:
            self.original = imaging.from_png_bytes(original)
            self._original_png = original
            self._original_ref = self.original
        self.origin = snapshot.get("origin", "none")
        self.filename = snapshot.get("filename")
        self.has_expansion = bool(snapshot.get("has_expansion"))
        self.expansion = dict(snapshot.get("expansion") or {})

    def checkpoint(self, label: str) -> None:
        """Remember the document as it is now, before changing it."""
        if self.image is None:
            return
        self.history.append(self._snapshot(label))
        del self.history[:-HISTORY_LIMIT]
        self.future.clear()

    def undo(self) -> typing.Optional[str]:
        if not self.history:
            return None
        snapshot = self.history.pop()
        self.future.append(self._snapshot(snapshot["label"]))
        del self.future[:-HISTORY_LIMIT]
        self._restore(snapshot)
        return snapshot["label"]

    def redo(self) -> typing.Optional[str]:
        if not self.future:
            return None
        snapshot = self.future.pop()
        self.history.append(self._snapshot(snapshot["label"]))
        del self.history[:-HISTORY_LIMIT]
        self._restore(snapshot)
        return snapshot["label"]

    # -- editing -----------------------------------------------------------

    def load(
        self,
        image: Image.Image,
        origin: str,
        filename: typing.Optional[str] = None,
    ) -> None:
        """Replace the document. Callers checkpoint first if it can be undone."""
        self.image = imaging.to_rgba(image)
        self.mask = None
        self.original = self.image
        self.origin = origin
        self.filename = filename
        self.has_expansion = False
        self.expansion = {}

    def clear(self) -> None:
        self.image = None
        self.mask = None
        self.original = None
        self.origin = "none"
        self.filename = None
        self.has_expansion = False
        self.expansion = {}

    def commit(self, image: Image.Image, mask: typing.Optional[Image.Image]) -> None:
        """Take the canvas's word for image and mask, keeping them in step."""
        self.image = imaging.to_rgba(image)
        if imaging.mask_is_empty(mask):
            self.mask = None
        elif mask.size != self.image.size:
            self.mask = mask.resize(self.image.size, imaging.NEAREST)
        else:
            self.mask = mask


def ensure(state: typing.Any) -> Document:
    """gr.State starts as None; every callback goes through here."""
    return state if isinstance(state, Document) else Document()
