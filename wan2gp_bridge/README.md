# wan2gp-minipaint-bridge

The WanGP half of the Mini Paint WanGP integration.

`a1111-mini-paint-NEO` embeds a real WanGP in a Forge tab and adds WanGP
destinations to Mini Paint's **Send to** menu. Those destinations are not a
list this extension keeps: they are the answer WanGP itself gives, for the
model and the mode the user is actually looking at, in the browser tab they
are actually looking at it in. Something has to be inside WanGP to give that
answer and to put the picture where it belongs. That is this plugin.

It is deliberately small. It adds no generation UI, changes no generation
behaviour, touches no WanGP setting, and never presses Generate. It answers
two questions and performs one action:

* *what image inputs can this exact page accept right now?*
* *here is one PNG; put it in the start frame / the end frame / the reference
  list, and prove that you did.*

It also carries a dark stylesheet, because the embedded WanGP is a separate
document and nothing the Forge page wears reaches into it.

## Why it exists at all

The alternative would be for Mini Paint to reach into the WanGP page from the
outside: find the upload widget by its position or its label, drop a file on
it, hope. That breaks the first time WanGP rearranges anything, it cannot tell
an input that is switched on from one that merely exists, and it cannot prove
that what WanGP will generate from is the image that was sent. WanGP has a
supported plugin API, so the compatibility problem lives here, on WanGP's side
of the boundary, where it can be version-gated and fixed in one file.

The one file is `compatibility.py`. It is the only place in the whole
integration that knows a WanGP element id, setting name or flag letter. If a
WanGP release moves an input, that is the file to correct, and nothing in
Forge, in the browser or in Mini Paint changes.

## What it will not do

* It will not run without the Mini Paint integration. WanGP launched by hand
  has no `MINIPAINT_WANGP_INSTANCE_ID` in its environment, and the plugin adds
  nothing to the UI at all.
* It will not accept a file path. The browser sends a 32-character hex id and
  nothing else; the path is built here, from a root the launcher supplied, and
  an id of any other shape is rejected rather than repaired.
* It will not guess. If a WanGP build does not expose an input the bridge
  needs, the handshake says `ready=false` with `BRIDGE_COMPONENT_INCOMPATIBLE`
  and the Send menu offers nothing, rather than sending into something that
  looked about right.
* It will not silently replace your reference images. A reference send reads
  the list, checks the capacity, appends, and verifies the count.

## Installation

The Mini Paint setup wizard does this for you: step 4 of the WanGP tab's setup
copies this folder into your WanGP installation and checks the version. It
only ever writes inside its own folder.

By hand, if you prefer:

```
cp -r wan2gp_bridge/wan2gp-minipaint-bridge <your WanGP>/plugins/
```

Then restart WanGP. Plugins are loaded once at startup, so copying files over
a running WanGP does not activate them - and updating the bridge while WanGP
is running needs a restart for the same reason.

The installed layout:

```
<WanGP>/plugins/wan2gp-minipaint-bridge/
    __init__.py            what a plugin loader imports
    plugin.py              the hooks, and the single event everything runs on
    compatibility.py       the only file that knows WanGP's internals
    receiver_state.py      normalised live state, and its fingerprint
    receiver_adapters.py   how each input is read, filled and verified
    handoff.py             validating the PNG that Forge left behind an id
    bridge_ui.py           three invisible Gradio components
    bridge_js.py           the script that runs in the WanGP document
    protocol.py            the shared vocabulary, copied verbatim
    theme.css              the dark theme
    plugin_info.json       name, version, protocol, compatibility range
```

`protocol.py` is a byte-for-byte copy of the shared block of
`minipaint_neo/wangp/protocol.py`. It is duplicated rather than imported
because this code runs in WanGP's Python environment and may import nothing
from the extension; `tests/test_wangp_protocol.py` holds the two copies to
each other so they cannot drift.

## Compatibility

`plugin_info.json` declares a Wan2GP version range, but that range is only an
early filter. Real compatibility is functional and is decided at startup: the
bridge asks the plugin API for each component it needs, records which ones it
actually got, and reports the result in its handshake. A build that resolves
everything is ready; a build that does not names what is missing.

Adding a new receiver - a control image, a positioned reference - is three
declarations and no new machinery: a row in `compatibility.COMPONENTS`, a row
in `RECEIVER_COMPONENTS` and `SELECTION_RULES`, and a small subclass in
`receiver_adapters.py`. Mini Paint already knows every logical receiver id in
the protocol and simply never offers the ones this bridge does not publish.

## Before trusting it against a new WanGP

The element ids and flag letters in `compatibility.py` were written from
WanGP's documented media-input naming, not from a running build, and every one
of them carries a `VERIFY ON A REAL INSTALL` comment. The checks that matter,
once per WanGP revision:

1. The elem_ids at the top of `compatibility.py` resolve through
   `request_component`.
2. The flag letters in `SELECTION_RULES` match what the visible selectors
   actually put in `image_prompt_type` and `video_prompt_type`.
3. For each of start, end and reference: send an image with the bridge, look
   at the UI, then press **Generate** *without touching the field*, and
   confirm the generation used the inserted image. If a dependent control
   needs an official event to fire, it goes in that adapter's
   `downstream_updates`.

## Licence

MIT, with the rest of `a1111-mini-paint-NEO`.
