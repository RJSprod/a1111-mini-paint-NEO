"""The document the Canvas is editing: layers on a canvas, a mask, and
enough history to undo the big steps.

A layer is an RGBA picture at an offset on the document canvas. A picture
that arrives becomes "Layer 1" over a white "Background" of the same size,
so the canvas has an edge that can be seen and a layer can be dragged over
it, hidden or deleted without the canvas losing its shape. ``image`` is the
composite of the visible layers, kept current, and it is what the canvas
shows and what gets sent. One or several layers are selected at a time, the
way a layers panel works; the primary one (``active``) is the last picked,
and moving, resizing, fading, duplicating, deleting and merging act on the
whole selection. A resized layer keeps the pixels it had before its first
resize, so resizing again starts from those rather than from a resample.
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
BACKDROP = (255, 255, 255, 255)
# A side longer than this is past what a browser canvas holds comfortably.
MAX_LAYER_SIDE = 8192

try:  # Pillow >= 9.1
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - older Pillow
    LANCZOS = Image.LANCZOS


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
        source: typing.Optional[Image.Image] = None,
        scale: float = 1.0,
    ) -> None:
        self.image = imaging.to_rgba(image)
        self.x = int(x)
        self.y = int(y)
        self.name = name
        self.visible = bool(visible)
        self.opacity = max(0, min(100, int(opacity)))
        # The pixels before the first resize, and the size relative to them.
        self.source = imaging.to_rgba(source) if source is not None else None
        self.scale = float(scale) if scale and scale > 0 else 1.0

    @property
    def percent(self) -> int:
        """The size, as a percentage of the pixels the layer started with."""
        return int(round(self.scale * 100))

    def resize(self, percent: float) -> bool:
        """This layer at a percentage of the size it had before it was first
        resized, keeping its centre where it is. False when nothing changed."""
        try:
            factor = float(percent) / 100.0
        except (TypeError, ValueError):
            return False
        factor = max(0.01, min(40.0, factor))
        base = self.source if self.source is not None else self.image
        width = max(1, int(round(base.width * factor)))
        height = max(1, int(round(base.height * factor)))
        if (width, height) == self.image.size:
            return False
        if max(width, height) > MAX_LAYER_SIDE:
            raise ValueError(f"{width} × {height} is past what a browser canvas holds comfortably; keep a side under {MAX_LAYER_SIDE}px.")
        centre_x = self.x + self.image.width / 2
        centre_y = self.y + self.image.height / 2
        if (width, height) == base.size:
            self.image = base
            self.source = None
            self.scale = 1.0
        else:
            self.image = base.resize((width, height), LANCZOS)
            self.source = base
            self.scale = factor
        self.x = int(round(centre_x - width / 2))
        self.y = int(round(centre_y - height / 2))
        return True

    @property
    def size(self) -> typing.Tuple[int, int]:
        return self.image.size

    @property
    def box(self) -> typing.Tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.image.width, self.y + self.image.height)

    def copy(self, name: typing.Optional[str] = None) -> "Layer":
        return Layer(self.image.copy(), self.x, self.y, name or self.name, self.visible, self.opacity, self.source, self.scale)

    def snapshot(self) -> dict:
        return {
            "png": imaging.to_png_bytes(self.image),
            "x": self.x,
            "y": self.y,
            "name": self.name,
            "visible": self.visible,
            "opacity": self.opacity,
            "source": imaging.to_png_bytes(self.source) if self.source is not None else None,
            "scale": self.scale,
        }

    @classmethod
    def restore(cls, data: dict) -> "Layer":
        source = data.get("source")
        return cls(
            imaging.from_png_bytes(data["png"]),
            data["x"],
            data["y"],
            data["name"],
            data["visible"],
            data["opacity"],
            imaging.from_png_bytes(source) if source else None,
            data.get("scale", 1.0),
        )

    def describe(self) -> str:
        parts = [f"{self.name}: {self.image.width} × {self.image.height} at ({self.x}, {self.y})"]
        if self.percent != 100:
            parts.append(f"{self.percent}% size")
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
        self.selected: typing.List[int] = []
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
        # The layers panel as last sent to the browser, so it is only re-sent
        # when it changed.
        self.layer_list_sent: typing.Optional[str] = None
        # What the Send button does, chosen in Options: "Auto" or a destination.
        self.destination: str = "Auto"
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

    def selected_indices(self) -> typing.List[int]:
        """The selection, in stacking order, always including the primary."""
        if not self.layers:
            return []
        active = self.active_layer  # clamps
        chosen = {index for index in self.selected if 0 <= index < len(self.layers)}
        chosen.add(self.active)
        self.selected = sorted(chosen)
        return list(self.selected)

    def selected_layers(self) -> typing.List[Layer]:
        return [self.layers[index] for index in self.selected_indices()]

    def selected_names(self) -> typing.List[str]:
        return [layer.name for layer in self.selected_layers()]

    def index_of(self, name: str) -> typing.Optional[int]:
        for index, layer in enumerate(self.layers):
            if layer.name == name:
                return index
        return None

    def select(self, name: str) -> bool:
        """Only this layer, as the primary."""
        index = self.index_of(name)
        if index is None:
            return False
        self.active = index
        self.selected = [index]
        return True

    def toggle_selected(self, name: str) -> bool:
        """Add this layer to the selection, or take it out; the primary
        follows the last one added, and the selection never empties."""
        index = self.index_of(name)
        if index is None:
            return False
        chosen = set(self.selected_indices())
        if index in chosen and len(chosen) > 1:
            chosen.discard(index)
            if self.active == index:
                self.active = max(chosen)
        else:
            chosen.add(index)
            self.active = index
        self.selected = sorted(chosen)
        return True

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

    def next_layer_name(self) -> str:
        """Layer 1, Layer 2, ... counting the layers over the Background."""
        count = sum(1 for layer in self.layers if layer.name != BASE_NAME)
        return self.unique_name(f"Layer {count + 1}")

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

    def _reindex(self, keep: typing.Iterable[Layer], active: typing.Optional[Layer]) -> None:
        """After the layer list changed: the selection follows the layers
        that survived, the primary the layer given (or the nearest)."""
        survivors = [layer for layer in keep if layer in self.layers]
        self.selected = sorted(self.layers.index(layer) for layer in survivors)
        if active is not None and active in self.layers:
            self.active = self.layers.index(active)
        elif self.selected:
            self.active = self.selected[-1]
        else:
            self.active = max(0, min(self.active, len(self.layers) - 1))
            self.selected = [self.active] if self.layers else []

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
            "selected": list(self.selected_indices()),
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
        self.selected = [int(index) for index in snapshot.get("selected", [])]
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
        """Replace the document with one picture: Layer 1, over a white
        Background of the same size. Callers checkpoint first if it can be undone."""
        picture = imaging.to_rgba(image)
        backdrop = Image.new("RGBA", picture.size, BACKDROP)
        self.layers = [Layer(backdrop, 0, 0, BASE_NAME), Layer(picture, 0, 0, "Layer 1")]
        self.active = 1
        self.selected = [1]
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
        self.selected = []
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
            self.selected = [0]
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
        """A new layer above the primary one; it becomes the selection."""
        layer = Layer(image, x, y, self.unique_name(name) if name else self.next_layer_name())
        index = min(self.active + 1, len(self.layers)) if self.layers else 0
        self.layers.insert(index, layer)
        self.active = index
        self.selected = [index]
        self._touch()
        return layer

    def delete_selected(self) -> typing.List[str]:
        """Every selected layer, as long as one layer is left."""
        chosen = self.selected_layers()
        if len(chosen) >= len(self.layers):
            chosen = chosen[1:]  # the bottom-most selected one stays
        if not chosen:
            return []
        below = None
        for layer in chosen:
            index = self.layers.index(layer)
            below = self.layers[index - 1] if index > 0 else None
            self.layers.remove(layer)
        nearest = below if below in self.layers else (self.layers[0] if self.layers else None)
        self._reindex([nearest] if nearest else [], nearest)
        self._touch()
        return [layer.name for layer in chosen]

    def move_selected(self, dx: int, dy: int) -> None:
        for layer in self.selected_layers():
            layer.x += int(dx)
            layer.y += int(dy)
        self._touch()

    def center_selected(self) -> None:
        """Each selected layer to the middle of the canvas: the way back for
        one that was dragged out of view."""
        if self.canvas_size is None:
            return
        width, height = self.canvas_size
        for layer in self.selected_layers():
            layer.x = (width - layer.image.width) // 2
            layer.y = (height - layer.image.height) // 2
        self._touch()

    def set_visible(self, name: str, visible: typing.Optional[bool] = None) -> typing.Optional[bool]:
        index = self.index_of(name)
        if index is None:
            return None
        layer = self.layers[index]
        layer.visible = (not layer.visible) if visible is None else bool(visible)
        self._touch()
        return layer.visible

    def scale_selected(self, percent: typing.Any) -> bool:
        """Every selected layer at a percentage of its original size, each
        keeping its centre. False when nothing changed."""
        changed = False
        for layer in self.selected_layers():
            changed = layer.resize(percent) or changed
        if changed:
            self._touch()
        return changed

    def set_opacity(self, value: typing.Any) -> None:
        try:
            opacity = max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError):
            return
        for layer in self.selected_layers():
            layer.opacity = opacity
        self._touch()

    def rename_active(self, name: str) -> typing.Optional[str]:
        layer = self.active_layer
        if layer is None:
            return None
        layer.name = self.unique_name(name, ignore=layer)
        self.layer_version += 1
        return layer.name

    def reorder(self, name: str, step: int) -> bool:
        """Swap one layer with its neighbour: +1 is up (towards the front)."""
        index = self.index_of(name)
        if index is None:
            return False
        target = index + int(step)
        if not (0 <= target < len(self.layers)) or target == index:
            return False
        chosen = self.selected_layers()
        active = self.active_layer
        self.layers[index], self.layers[target] = self.layers[target], self.layers[index]
        self._reindex(chosen, active)
        self._touch()
        return True

    def merge_selected(self) -> typing.Optional[str]:
        """Two or more selected layers into one, as the canvas shows them;
        one selected layer merges into the layer below it."""
        chosen = self.selected_layers()
        if len(chosen) < 2:
            if self.active < 1:
                return None
            chosen = [self.layers[self.active - 1], self.layers[self.active]]
        merged = chosen[0]
        for upper in chosen[1:]:
            image, x, y = imaging.merge_layers(merged, upper)
            merged = Layer(image, x, y, chosen[0].name, True, 100)
        position = self.layers.index(chosen[0])
        for layer in chosen:
            self.layers.remove(layer)
        self.layers.insert(position, merged)
        self.active = position
        self.selected = [position]
        self._touch()
        return merged.name

    def duplicate_selected(self) -> typing.List[Layer]:
        copies = []
        for layer in reversed(self.selected_layers()):
            copy = layer.copy(self.unique_name(f"{layer.name} copy"))
            self.layers.insert(self.layers.index(layer) + 1, copy)
            copies.append(copy)
        copies.reverse()
        self._reindex(copies, copies[-1] if copies else None)
        self._touch()
        return copies

    def flatten(self) -> int:
        """Every visible layer into one; hidden layers are dropped."""
        if not self.has_image:
            return 0
        count = len(self.layers)
        flat = imaging.composite(self.layers, self.canvas_size)
        self.layers = [Layer(flat, 0, 0, BASE_NAME)]
        self.active = 0
        self.selected = [0]
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
        survivors: typing.Dict[int, Layer] = {}
        for index, layer in enumerate(self.layers):
            piece = imaging.layer_pixels_in_box(layer.image, layer.x, layer.y, box)
            if piece is None:
                continue
            image, px, py = piece
            survivors[index] = Layer(image, px - x0, py - y0, layer.name, layer.visible, layer.opacity)
            kept.append(survivors[index])
        if not kept:
            kept = [Layer(Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0)), 0, 0, BASE_NAME)]
        chosen = [survivors[index] for index in self.selected_indices() if index in survivors]
        active = survivors.get(self.active)
        self.layers = kept
        self._reindex(chosen, active)
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
        return (tuple(self.selected_indices()), self.layer_version)

    def preview_payload(self) -> str:
        """The selected layers, as one picture, the way the browser shows
        them while dragging."""
        chosen = self.selected_layers()
        if not chosen:
            return ""
        x0 = min(layer.x for layer in chosen)
        y0 = min(layer.y for layer in chosen)
        x1 = max(layer.x + layer.image.width for layer in chosen)
        y1 = max(layer.y + layer.image.height for layer in chosen)
        shifted = [Layer(layer.image, layer.x - x0, layer.y - y0, layer.name, layer.visible, layer.opacity) for layer in chosen]
        picture = imaging.composite(shifted, (x1 - x0, y1 - y0))
        return json.dumps(
            {
                "src": imaging.to_data_url(picture),
                "x": x0,
                "y": y0,
                "w": x1 - x0,
                "h": y1 - y0,
                "opacity": 100 if any(layer.visible for layer in chosen) else 0,
                "name": ", ".join(layer.name for layer in chosen),
            }
        )

    def underlay_payload(self) -> str:
        """The other layers, for the canvas to show under the layers being dragged."""
        chosen = set(self.selected_indices())
        if len(self.layers) <= len(chosen) or self.canvas_size is None:
            return ""
        others = [layer for index, layer in enumerate(self.layers) if index not in chosen]
        return imaging.to_data_url(imaging.composite(others, self.canvas_size))

    def layer_rows(self) -> typing.List[dict]:
        """What a layers panel shows, top layer first."""
        chosen = set(self.selected_indices())
        rows = []
        for index in range(len(self.layers) - 1, -1, -1):
            layer = self.layers[index]
            rows.append(
                {
                    "name": layer.name,
                    "visible": layer.visible,
                    "selected": index in chosen,
                    "active": index == self.active,
                    "size": f"{layer.image.width} × {layer.image.height}",
                    "at": f"({layer.x}, {layer.y})",
                    "opacity": layer.opacity,
                    "percent": layer.percent,
                    "top": index == len(self.layers) - 1,
                    "bottom": index == 0,
                }
            )
        return rows


def ensure(state: typing.Any) -> Document:
    """gr.State starts as None; every callback goes through here."""
    return state if isinstance(state, Document) else Document()
