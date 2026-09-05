"""The document the Canvas is editing: layers on a canvas, a mask, and
enough history to undo the big steps.

A layer is an RGBA picture at an offset on the document canvas; the base
layer is the picture that arrived. ``image`` is the composite of the visible
layers, kept current, and it is what the canvas shows and what gets sent.
Strokes are undone by the canvas itself. What the history holds is the
structural work - receive, open, apply crop, expand, clear, invert, reset,
and every layer operation - because those replace the canvas's contents
wholesale.
"""

from __future__ import annotations

import json
import typing

from PIL import Image

from . import imaging

HISTORY_LIMIT = 6
BASE_NAME = "Background"


class Layer:
    """One picture on the document canvas."""

    def __init__(
        self,
        image: Image.Image,
        x: int = 0,
        y: int = 0,
        name: str = "Layer",
        visible: bool = True,
        opacity: int = 100,
    ) -> None:
        self.image = imaging.to_rgba(image)
        self.x = int(x)
        self.y = int(y)
        self.name = name
        self.visible = bool(visible)
        self.opacity = max(0, min(100, int(opacity)))

    @property
    def size(self) -> typing.Tuple[int, int]:
        return self.image.size

    @property
    def box(self) -> typing.Tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.image.width, self.y + self.image.height)

    def copy(self, name: typing.Optional[str] = None) -> "Layer":
        return Layer(self.image.copy(), self.x, self.y, name or self.name, self.visible, self.opacity)

    def snapshot(self) -> dict:
        return {
            "png": imaging.to_png_bytes(self.image),
            "x": self.x,
            "y": self.y,
            "name": self.name,
            "visible": self.visible,
            "opacity": self.opacity,
        }

    @classmethod
    def restore(cls, data: dict) -> "Layer":
        return cls(imaging.from_png_bytes(data["png"]), data["x"], data["y"], data["name"], data["visible"], data["opacity"])

    def describe(self) -> str:
        parts = [f"{self.name}: {self.image.width} × {self.image.height} at ({self.x}, {self.y})"]
        if not self.visible:
            parts.append("hidden")
        if self.opacity < 100:
            parts.append(f"{self.opacity}% opacity")
        return ", ".join(parts)


class Document:
    """Layers, the composite they make, mask coverage, and where it came from."""

    def __init__(self) -> None:
        self.layers: typing.List[Layer] = []
        self.active: int = 0
        self.canvas_size: typing.Optional[typing.Tuple[int, int]] = None
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
        # A send into Inpaint writes the mask in a later step; this is what
        # that step needs to know, and it is consumed by it.
        self.pending_send: typing.Optional[dict] = None
        # Bumped whenever a layer changes, so the browser's drag preview is
        # only re-sent when it is stale.
        self.layer_version: int = 0
        self.preview_sent: typing.Optional[tuple] = None
        self._original_png: typing.Optional[bytes] = None
        self._original_ref: typing.Optional[Image.Image] = None

    # -- state -------------------------------------------------------------

    @property
    def size(self) -> typing.Optional[typing.Tuple[int, int]]:
        return self.canvas_size

    @property
    def has_image(self) -> bool:
        return bool(self.layers) and self.canvas_size is not None

    @property
    def has_mask(self) -> bool:
        return not imaging.mask_is_empty(self.mask)

    @property
    def active_layer(self) -> typing.Optional[Layer]:
        if not self.layers:
            return None
        self.active = max(0, min(self.active, len(self.layers) - 1))
        return self.layers[self.active]

    @property
    def layered(self) -> bool:
        """More than the picture that arrived: another layer, or the base moved or faded."""
        if len(self.layers) > 1:
            return True
        base = self.active_layer
        return bool(base) and (base.x != 0 or base.y != 0 or base.opacity < 100 or base.size != self.canvas_size)

    def layer_names(self) -> typing.List[str]:
        return [layer.name for layer in self.layers]

    def visible_names(self) -> typing.List[str]:
        return [layer.name for layer in self.layers if layer.visible]

    def original_size_text(self) -> str:
        source = self.original if self.original is not None else self.image
        return f"{source.width}x{source.height}" if source is not None else ""

    def describe(self) -> str:
        if not self.has_image:
            return "No image"
        width, height = self.canvas_size
        parts = [f"{width} × {height}"]
        if len(self.layers) > 1:
            parts.append(f"{len(self.layers)} layers")
        if self.has_expansion:
            parts.append("expanded")
        if self.has_mask:
            parts.append("mask")
        if self.filename:
            parts.append(self.filename)
        elif self.origin not in ("none", "file"):
            parts.append(f"from {self.origin}")
        return " · ".join(parts)

    # -- the composite -------------------------------------------------------

    def recomposite(self) -> None:
        if not self.layers or self.canvas_size is None:
            self.image = None
            return
        self.image = imaging.composite(self.layers, self.canvas_size)

    def _touch(self) -> None:
        self.layer_version += 1
        self.recomposite()

    def unique_name(self, wanted: str, ignore: typing.Optional[Layer] = None) -> str:
        wanted = (wanted or "Layer").strip() or "Layer"
        taken = {layer.name for layer in self.layers if layer is not ignore}
        if wanted not in taken:
            return wanted
        number = 2
        while f"{wanted} {number}" in taken:
            number += 1
        return f"{wanted} {number}"

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
        memory per open tab. The composite is not stored; it is rebuilt.
        """
        return {
            "label": label,
            "layers": [layer.snapshot() for layer in self.layers],
            "active": self.active,
            "canvas_size": self.canvas_size,
            "mask": imaging.to_png_bytes(self.mask) if self.has_mask else None,
            "original": self._original_bytes(),
            "origin": self.origin,
            "filename": self.filename,
            "has_expansion": self.has_expansion,
            "expansion": dict(self.expansion),
        }

    def _restore(self, snapshot: dict) -> None:
        mask = snapshot.get("mask")
        original = snapshot.get("original")
        self.layers = [Layer.restore(data) for data in snapshot.get("layers", [])]
        self.active = int(snapshot.get("active", 0))
        self.canvas_size = tuple(snapshot["canvas_size"]) if snapshot.get("canvas_size") else None
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
        self._touch()

    def checkpoint(self, label: str) -> None:
        """Remember the document as it is now, before changing it."""
        if not self.has_image:
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
        """Replace the document with one picture. Callers checkpoint first if it can be undone."""
        picture = imaging.to_rgba(image)
        self.layers = [Layer(picture, 0, 0, BASE_NAME)]
        self.active = 0
        self.canvas_size = picture.size
        self.mask = None
        self.original = picture
        self.origin = origin
        self.filename = filename
        self.has_expansion = False
        self.expansion = {}
        self._touch()

    def clear(self) -> None:
        self.layers = []
        self.active = 0
        self.canvas_size = None
        self.image = None
        self.mask = None
        self.original = None
        self.origin = "none"
        self.filename = None
        self.has_expansion = False
        self.expansion = {}
        self.layer_version += 1

    def commit(self, image: typing.Optional[Image.Image], mask: typing.Optional[Image.Image]) -> None:
        """Take the canvas's word for the mask, and for the picture too while
        the document is one flat picture and the canvas shows a different one."""
        if image is not None and not self.layered and not imaging.images_equal(image, self.image):
            picture = imaging.to_rgba(image)
            name = self.layers[0].name if self.layers else BASE_NAME
            self.layers = [Layer(picture, 0, 0, name)]
            self.active = 0
            self.canvas_size = picture.size
            self._touch()
        if self.canvas_size is None:
            self.mask = None
        elif imaging.mask_is_empty(mask):
            self.mask = None
        elif mask.size != self.canvas_size:
            self.mask = mask.resize(self.canvas_size, imaging.NEAREST)
        else:
            self.mask = mask

    # -- layers ------------------------------------------------------------

    def add_layer(self, image: Image.Image, x: int, y: int, name: typing.Optional[str] = None) -> Layer:
        """A new layer above the active one, which becomes the active one."""
        layer = Layer(image, x, y, self.unique_name(name or f"Layer {len(self.layers) + 1}"))
        index = min(self.active + 1, len(self.layers)) if self.layers else 0
        self.layers.insert(index, layer)
        self.active = index
        self._touch()
        return layer

    def delete_active(self) -> typing.Optional[str]:
        if len(self.layers) < 2:
            return None
        removed = self.layers.pop(self.active)
        self.active = max(0, min(self.active, len(self.layers) - 1))
        self._touch()
        return removed.name

    def move_active(self, dx: int, dy: int) -> None:
        layer = self.active_layer
        if layer is None:
            return
        layer.x += int(dx)
        layer.y += int(dy)
        self._touch()

    def set_visibility(self, visible_names: typing.Iterable[str]) -> None:
        wanted = set(visible_names or [])
        for layer in self.layers:
            layer.visible = layer.name in wanted
        self._touch()

    def set_opacity(self, value: typing.Any) -> None:
        layer = self.active_layer
        if layer is None:
            return
        try:
            layer.opacity = max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError):
            return
        self._touch()

    def rename_active(self, name: str) -> typing.Optional[str]:
        layer = self.active_layer
        if layer is None:
            return None
        layer.name = self.unique_name(name, ignore=layer)
        self.layer_version += 1
        return layer.name

    def reorder_active(self, step: int) -> bool:
        """Swap the active layer with its neighbour: +1 is up (towards the front)."""
        target = self.active + int(step)
        if not self.layers or not (0 <= target < len(self.layers)) or target == self.active:
            return False
        self.layers[self.active], self.layers[target] = self.layers[target], self.layers[self.active]
        self.active = target
        self._touch()
        return True

    def merge_down(self) -> typing.Optional[str]:
        """The active layer onto the one below it, as the canvas shows them."""
        if self.active < 1 or not self.layers:
            return None
        upper = self.layers[self.active]
        lower = self.layers[self.active - 1]
        merged_image, x, y = imaging.merge_layers(lower, upper)
        merged = Layer(merged_image, x, y, lower.name, True, 100)
        self.layers[self.active - 1] = merged
        del self.layers[self.active]
        self.active -= 1
        self._touch()
        return merged.name

    def duplicate_active(self) -> typing.Optional[Layer]:
        layer = self.active_layer
        if layer is None:
            return None
        copy = layer.copy(self.unique_name(f"{layer.name} copy"))
        self.layers.insert(self.active + 1, copy)
        self.active += 1
        self._touch()
        return copy

    def flatten(self) -> int:
        """Every visible layer into one; hidden layers are dropped."""
        if not self.has_image:
            return 0
        count = len(self.layers)
        flat = imaging.composite(self.layers, self.canvas_size)
        self.layers = [Layer(flat, 0, 0, BASE_NAME)]
        self.active = 0
        self._touch()
        return count

    def base_full(self) -> Image.Image:
        """The base layer as it sits on the canvas, canvas-sized."""
        base = self.layers[0]
        return imaging.composite([Layer(base.image, base.x, base.y, base.name, True, 100)], self.canvas_size)

    # -- geometry ----------------------------------------------------------

    def crop(self, box: typing.Tuple[int, int, int, int], mask: typing.Optional[Image.Image]) -> None:
        """Keep what is inside the box, on every layer."""
        x0, y0, x1, y1 = box
        kept: typing.List[Layer] = []
        new_active = 0
        for index, layer in enumerate(self.layers):
            piece = imaging.layer_pixels_in_box(layer.image, layer.x, layer.y, box)
            if piece is None:
                continue
            image, px, py = piece
            if index <= self.active:
                new_active = len(kept)
            kept.append(Layer(image, px - x0, py - y0, layer.name, layer.visible, layer.opacity))
        if not kept:
            kept = [Layer(Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0)), 0, 0, BASE_NAME)]
        self.layers = kept
        self.active = min(new_active, len(kept) - 1)
        self.canvas_size = (x1 - x0, y1 - y0)
        self.mask = mask.crop(box) if not imaging.mask_is_empty(mask) else None
        self.has_expansion = False
        self.expansion = {}
        self._touch()

    def expand(self, sides: typing.Sequence[int], expanded_base: Image.Image, new_mask: typing.Optional[Image.Image]) -> None:
        """The canvas grew: the base layer is the expanded picture, every
        other layer keeps its place relative to the old canvas."""
        left, _right, top, _bottom = sides
        base = self.layers[0]
        self.layers[0] = Layer(expanded_base, 0, 0, base.name, base.visible, base.opacity)
        for layer in self.layers[1:]:
            layer.x += int(left)
            layer.y += int(top)
        self.canvas_size = expanded_base.size
        self.mask = new_mask if not imaging.mask_is_empty(new_mask) else None
        self._touch()

    # -- what the browser needs to drag a layer ------------------------------

    def preview_key(self) -> tuple:
        return (self.active, self.layer_version)

    def preview_payload(self) -> str:
        """The active layer as the browser shows it while dragging."""
        layer = self.active_layer
        if layer is None:
            return ""
        return json.dumps(
            {
                "src": imaging.to_data_url(layer.image),
                "x": layer.x,
                "y": layer.y,
                "w": layer.image.width,
                "h": layer.image.height,
                "opacity": layer.opacity if layer.visible else 0,
                "name": layer.name,
            }
        )

    def underlay_payload(self) -> str:
        """The other layers, for the canvas to show under a layer being dragged."""
        if len(self.layers) < 2 or self.canvas_size is None:
            return ""
        others = [layer for index, layer in enumerate(self.layers) if index != self.active]
        return imaging.to_data_url(imaging.composite(others, self.canvas_size))


def ensure(state: typing.Any) -> Document:
    """gr.State starts as None; every callback goes through here."""
    return state if isinstance(state, Document) else Document()
