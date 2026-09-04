"""The document the Canvas is editing, and enough history to undo the big steps.

Strokes are undone by the editor itself. What this stack holds is the
structural work - open, receive, crop, expand, clear, reset - because those
change the document's dimensions or replace it wholesale, and the component's
own history does not survive that.
"""

from __future__ import annotations

import typing

from PIL import Image

from . import imaging

HISTORY_LIMIT = 8


class Document:
    """Working image, mask coverage, and where they came from."""

    def __init__(self) -> None:
        self.image: typing.Optional[Image.Image] = None
        self.mask: typing.Optional[Image.Image] = None
        self.original: typing.Optional[Image.Image] = None
        self.origin: str = "none"
        self.filename: typing.Optional[str] = None
        self.has_expansion: bool = False
        self.last_expansion: dict = {}
        self.history: typing.List[dict] = []
        self.future: typing.List[dict] = []
        self.last_send: str = ""

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

    def describe(self) -> str:
        if self.image is None:
            return "No image"
        width, height = self.image.size
        parts = [f"{width} x {height}"]
        if self.has_mask:
            parts.append("mask")
        if self.has_expansion:
            parts.append("expanded")
        if self.filename:
            parts.append(self.filename)
        return " - ".join(parts)

    # -- history -----------------------------------------------------------

    def _snapshot(self, label: str) -> dict:
        """A whole document, small enough to keep eight of.

        PNG rather than the images themselves: a structural step is rare and
        slow anyway, and eight uncompressed 4K RGBA frames is a quarter of a
        gigabyte of somebody's tab.
        """
        return {
            "label": label,
            "image": imaging.to_png_bytes(self.image) if self.image is not None else None,
            "mask": imaging.to_png_bytes(self.mask) if self.has_mask else None,
            "origin": self.origin,
            "filename": self.filename,
            "has_expansion": self.has_expansion,
            "last_expansion": dict(self.last_expansion),
        }

    def _restore(self, snapshot: dict) -> None:
        image = snapshot.get("image")
        mask = snapshot.get("mask")
        self.image = imaging.from_png_bytes(image) if image else None
        self.mask = imaging.from_png_bytes(mask).convert("L") if mask else None
        self.origin = snapshot.get("origin", "none")
        self.filename = snapshot.get("filename")
        self.has_expansion = bool(snapshot.get("has_expansion"))
        self.last_expansion = dict(snapshot.get("last_expansion") or {})

    def checkpoint(self, label: str) -> None:
        """Remember the document as it is now, before changing it."""
        if self.image is None and not self.history:
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
        self.last_expansion = {}

    def commit(
        self,
        image: Image.Image,
        mask: typing.Optional[Image.Image],
    ) -> None:
        """Take the editor's word for image and mask, keeping them in step."""
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
