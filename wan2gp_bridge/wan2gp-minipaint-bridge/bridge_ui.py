"""The three invisible Gradio components the bridge talks through.

A postMessage cannot change a Gradio session. Only a Gradio event can, and an
event needs components: something to carry the request in, something to carry
the acknowledgement out, and something whose activation the browser can cause.
That is all this module builds - one textbox in, one textbox out, one button -
and it builds them inside WanGP's own Blocks so that the callback they fire is
an ordinary event on the page the user is looking at, with that page's live
values as its inputs.

Which is the point. Section 21.2 rules out setting a value in the DOM and
declaring victory, because what WanGP generates from is server-side session
state. Going through a real event means the values the callback reads belong
to the browser session that fired it - the session scoping of section 13 comes
free - and the values it returns are applied by Gradio itself, the same way a
human upload is applied.

Nothing here knows what a receiver is. It is handed the receiver components as
opaque outputs and the state components as opaque inputs; the compatibility
layer decided which ones those are and the adapters decide what to do with
them. If Gradio is somehow not importable this module says so by returning
None rather than raising, because a bridge that cannot build is a bridge that
is switched off, not a WanGP that fails to start.
"""

from __future__ import annotations

import dataclasses
import typing

try:
    import gradio as gr
except Exception:  # pragma: no cover - Gradio is WanGP's own dependency
    gr = None  # type: ignore[assignment]


#: The ids the bridge's own JavaScript looks up. They are ours, they are
#: prefixed, and they are the only element ids the browser side ever names.
TRIGGER_ELEM_ID = "minipaint_bridge_trigger"
REQUEST_ELEM_ID = "minipaint_bridge_request"
ACK_ELEM_ID = "minipaint_bridge_ack"

#: One class on all three, so theme.css can keep them out of the layout
#: without selecting anything of WanGP's.
BRIDGE_CLASS = "minipaint-bridge-io"


@dataclasses.dataclass
class BridgeControls:
    """The bridge's own components, and nothing of WanGP's."""

    request: typing.Any
    ack: typing.Any
    trigger: typing.Any


def build() -> typing.Optional[BridgeControls]:
    """Create the hidden request/ack pair and the trigger, or None.

    ``visible=False`` rather than a zero-size style: Gradio still renders the
    elements, so the ids resolve, and WanGP's layout never has to make room
    for a control the user is not meant to see.
    """
    if gr is None:
        return None
    try:
        request = gr.Textbox(
            value="",
            visible=False,
            interactive=True,
            elem_id=REQUEST_ELEM_ID,
            elem_classes=[BRIDGE_CLASS],
            label="Mini Paint bridge request",
            show_label=False,
        )
        ack = gr.Textbox(
            value="",
            visible=False,
            interactive=False,
            elem_id=ACK_ELEM_ID,
            elem_classes=[BRIDGE_CLASS],
            label="Mini Paint bridge acknowledgement",
            show_label=False,
        )
        trigger = gr.Button(
            "Mini Paint bridge",
            visible=False,
            elem_id=TRIGGER_ELEM_ID,
            elem_classes=[BRIDGE_CLASS],
        )
    except Exception:
        return None
    return BridgeControls(request=request, ack=ack, trigger=trigger)


def wire(
    controls: BridgeControls,
    handler: typing.Callable[..., typing.Any],
    state_components: typing.Sequence[typing.Any],
    receiver_components: typing.Sequence[typing.Any],
    deliver_js: str,
) -> bool:
    """Attach the one event the whole bridge runs on.

    The output list is the acknowledgement followed by every v1 receiver
    component, which is section 21.1's "wire a hidden bridge trigger to all v1
    receiver components as possible outputs and return no change for the rest".
    One event rather than one per receiver, because the request also has to be
    able to answer "what can you take right now", and that answer has to be
    computed from the same inputs in the same call as the apply.

    VERIFY ON A REAL INSTALL: ``.then(fn=None, ..., js=...)`` is how the
    acknowledgement gets back to the browser without a second round trip. If
    the installed Gradio rejects a null function on ``then``, replace it with
    an identity callback writing the same textbox - the JavaScript contract
    does not change either way.
    """
    if gr is None or controls is None:
        return False

    inputs = [controls.request, *state_components]
    outputs = [controls.ack, *receiver_components]

    try:
        event = controls.trigger.click(fn=handler, inputs=inputs, outputs=outputs, show_progress="hidden")
    except TypeError:
        # Older Gradio spells the progress argument differently, and it is a
        # cosmetic argument; the event itself is what matters.
        event = controls.trigger.click(fn=handler, inputs=inputs, outputs=outputs)

    try:
        event.then(fn=None, inputs=[controls.ack], outputs=[], js=deliver_js)
    except TypeError:
        event.then(fn=None, inputs=[controls.ack], outputs=[], _js=deliver_js)
    return True


def no_change(count: int) -> typing.List[typing.Any]:
    """``gr.update()`` once per receiver output: "leave this one alone".

    Returned for every receiver the request is not addressing, so that one
    event can own all of them without an apply to the start frame quietly
    clearing the reference list.
    """
    if gr is None:
        return [None] * max(0, int(count))
    return [gr.update() for _ in range(max(0, int(count)))]
