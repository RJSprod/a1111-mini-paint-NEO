"""The three invisible Gradio components the bridge talks through.

A postMessage cannot change a Gradio session. Only a Gradio event can, and an
event needs components: something to carry the request in, something to carry
the acknowledgement out, and something whose activation the browser can cause.
That is all this module builds - one textbox in, one textbox out, one button -
and it builds them inside WanGP's own Blocks so that the callback they fire is
an ordinary event on the page the user is looking at, with that page's live
values as its inputs.

"Inside WanGP's own Blocks" is literal, and it is the part that was wrong for
several rounds. A Gradio component created outside a ``with gr.Blocks()``
context is never part of any page: it gets an id, it can be named as an event's
input, and the page config still does not contain it, so the browser never
finds it and the bridge never answers. ``setup_ui`` runs before WanGP's Blocks
exist. The one sanctioned way to add a component to the page from a plugin is
``insert_after(target, builder)``, asked for during ``post_ui_setup``: WanGP
then calls the builder inside the target's own container, and everything the
builder creates is on the page. ``build`` below is written to be that builder's
body, and it is only correct when called from one.

WanGP builds its generator form twice - the Media Generator tab and the hidden
Edit tab - and asks every plugin's ``post_ui_setup`` once per form, so there
are two sets of these controls on a page. Each set gets element ids of its own
and the classes the browser script looks them up by; the script picks the set
whose surroundings are displayed.

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


#: The element-id prefixes of the bridge's own components. They are ours, they
#: are prefixed, and each set on a page gets its own suffix - see ``elem_id``.
COLUMN_ELEM_ID = "minipaint_bridge"
TRIGGER_ELEM_ID = "minipaint_bridge_trigger"
REQUEST_ELEM_ID = "minipaint_bridge_request"
ACK_ELEM_ID = "minipaint_bridge_ack"

#: One class on all three, so theme.css can keep them out of the layout
#: without selecting anything of WanGP's.
BRIDGE_CLASS = "minipaint-bridge-io"

#: The classes the browser script looks the controls up by. Classes rather
#: than ids because there is one set per form and WanGP builds two forms;
#: the script wants "the request box of the set that is on screen", and an id
#: can only ever name one of them.
COLUMN_CLASS = "minipaint-bridge-column"
REQUEST_CLASS = "minipaint-bridge-request"
ACK_CLASS = "minipaint-bridge-ack"
TRIGGER_CLASS = "minipaint-bridge-trigger"

#: Where the controls are asked to go, in order of preference. These are the
#: names WanGP hands ``post_ui_setup`` - its own local variable names for the
#: components - and ``insert_after`` takes the same names. Both are hidden
#: text fields inside the generator form, so the controls land inside the
#: form whose values the event reads.
PREFERRED_TARGETS = ("image_prompt_type", "video_prompt_type")


@dataclasses.dataclass
class BridgeControls:
    """The bridge's own components, and nothing of WanGP's."""

    request: typing.Any
    ack: typing.Any
    trigger: typing.Any
    column: typing.Any = None
    instance: int = 1


def elem_id(prefix: str, instance: int) -> str:
    """``prefix_N``: unique per set, recognisable to a person reading the DOM."""
    return f"{prefix}_{max(1, int(instance))}"


def target_for(handed: typing.Mapping[str, typing.Any]) -> str:
    """The component to sit after, chosen from what WanGP actually handed over.

    Preference first, then anything at all that was handed: the controls have
    to be *somewhere* on the page, and any component WanGP resolved for us is
    a component with a container to be inserted into.
    """
    names = [str(name) for name in handed] if isinstance(handed, typing.Mapping) else []
    for wanted in PREFERRED_TARGETS:
        if wanted in names:
            return wanted
    return names[0] if names else ""


def build(instance: int = 1) -> typing.Optional[BridgeControls]:
    """Create the hidden column holding the request/ack pair and the trigger.

    Call this only where Gradio is collecting components into a page - in
    practice, from the builder handed to WanGP's ``insert_after``. Called
    anywhere else it returns components that belong to no page, which is
    precisely the silent failure this module's docstring describes.

    One hidden column rather than three loose components, because WanGP's
    inserter takes exactly one new child from the container it ran the
    builder in and moves it behind the target. Three loose ones would leave
    two of them at the end of the container, out of order but present; a
    column keeps the set together and is what the browser script picks by.

    ``visible=False`` rather than a zero-size style: Gradio still renders the
    elements, so the classes resolve, and WanGP's layout never has to make
    room for a control the user is not meant to see.
    """
    if gr is None:
        return None
    try:
        with gr.Column(
            visible=False,
            elem_id=elem_id(COLUMN_ELEM_ID, instance),
            elem_classes=[COLUMN_CLASS],
        ) as column:
            request = gr.Textbox(
                value="",
                visible=False,
                interactive=True,
                elem_id=elem_id(REQUEST_ELEM_ID, instance),
                elem_classes=[BRIDGE_CLASS, REQUEST_CLASS],
                label="Mini Paint bridge request",
                show_label=False,
            )
            ack = gr.Textbox(
                value="",
                visible=False,
                interactive=False,
                elem_id=elem_id(ACK_ELEM_ID, instance),
                elem_classes=[BRIDGE_CLASS, ACK_CLASS],
                label="Mini Paint bridge acknowledgement",
                show_label=False,
            )
            trigger = gr.Button(
                "Mini Paint bridge",
                visible=False,
                elem_id=elem_id(TRIGGER_ELEM_ID, instance),
                elem_classes=[BRIDGE_CLASS, TRIGGER_CLASS],
            )
    except Exception:
        return None
    return BridgeControls(request=request, ack=ack, trigger=trigger, column=column, instance=instance)


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
    receiver components as possible outputs and return no change for the rest"
    - and, after those, the selector components a send may switch on
    (``receiver_components`` carries both, receivers first).
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


def update_for(change: typing.Any) -> typing.Any:
    """One switch update as Gradio takes it.

    A mapping is a property update - ``{"visible": True}`` to show the row a
    click on the selector would have shown - and becomes ``gr.update(**...)``;
    anything else is the component's new value (the radio's letter, the
    checkbox's True, the dropdown's choice, the rewritten letter string).
    """
    if isinstance(change, dict):
        return gr.update(**change) if gr is not None else dict(change)
    return change


def no_change(count: int) -> typing.List[typing.Any]:
    """``gr.update()`` once per receiver output: "leave this one alone".

    Returned for every receiver the request is not addressing, so that one
    event can own all of them without an apply to the start frame quietly
    clearing the reference list.
    """
    if gr is None:
        return [None] * max(0, int(count))
    return [gr.update() for _ in range(max(0, int(count)))]
