"""The script that runs inside the WanGP document, and its rules of contact.

The iframe and the Forge page are two documents. The only thing that crosses
between them is a postMessage, and the only thing this script will act on is a
message that passes every check at once: it came from exactly this origin -
which is the Forge origin, because WanGP is served through ``/wan2gp/`` on it
- from exactly ``window.parent``, small enough to read, speaking our protocol
version, carrying a type from the inbound whitelist, naming the channel this
page was introduced on, and carrying an object as its payload. Anything else
is dropped without a reply, because replying is itself information.

Nothing that arrives is ever executed, named as a function, or turned into a
path. A message can do exactly four things: ask who this page is, ask what it
can accept, ask for one handoff id to be applied to one named receiver, or ask
that a receiver already published in this page's own receiver list be scrolled
into view. The handoff id is 32 hex characters or it is not a handoff id; the
receiver must be one the server just described; the focus target must be an
element id this script published itself.

Everything that changes WanGP's state goes through the hidden Gradio trigger,
never through the DOM. The script fills its own request textbox and clicks its
own button - the request channel is the bridge's, not WanGP's - and every
value that reaches a receiver is returned by the Python callback so that
Gradio applies it the way it applies a human upload. The theme is injected
here too, in its own try/catch, because a stylesheet that fails to apply must
not take image handoff with it.

The script is generated from Python so that the protocol constants have one
definition rather than two: they are interpolated as JSON from ``protocol``.
"""

from __future__ import annotations

import json
import typing

try:
    from . import bridge_ui, protocol
except ImportError:  # pragma: no cover - depends on how WanGP imports plugins
    import bridge_ui  # type: ignore[no-redef]
    import protocol  # type: ignore[no-redef]


#: How many times the script will look for its own trigger before giving up.
#: The components exist by the time the parent says hello in practice; this
#: covers a slow first render. It is bounded on purpose - section 2 forbids
#: permanent polling, and a retry that never stops is polling with a nicer
#: name.
TRIGGER_ATTEMPTS = 20
TRIGGER_RETRY_MS = 250

#: What ``.then(js=...)`` runs with the acknowledgement textbox's value. It
#: names one method on one object and swallows its own failures, so a bridge
#: that is not present cannot turn into a console error on every event.
DELIVER_JS = "(ack) => { try { if (window.__minipaintBridge) { window.__minipaintBridge.deliver(ack); } } catch (error) {} }"


def configuration(theme_css: str = "") -> dict:
    """Everything the script needs that Python already knows."""
    return {
        "protocol": protocol.PROTOCOL,
        "types": {
            "hello": protocol.HELLO,
            "ready": protocol.READY,
            "getReceivers": protocol.GET_RECEIVERS,
            "receivers": protocol.RECEIVERS,
            "receiveImage": protocol.RECEIVE_IMAGE,
            "receiveResult": protocol.RECEIVE_RESULT,
            "focusReceiver": protocol.FOCUS_RECEIVER,
            "themeState": protocol.THEME_STATE,
        },
        "inbound": sorted(protocol.TO_BRIDGE),
        "receiverIds": list(protocol.RECEIVER_IDS),
        "maxEnvelopeBytes": protocol.MAX_ENVELOPE_BYTES,
        "requestElemId": bridge_ui.REQUEST_ELEM_ID,
        "ackElemId": bridge_ui.ACK_ELEM_ID,
        "triggerElemId": bridge_ui.TRIGGER_ELEM_ID,
        "triggerAttempts": TRIGGER_ATTEMPTS,
        "triggerRetryMs": TRIGGER_RETRY_MS,
        # Longer than the parent will wait, so in the ordinary case the parent
        # reports the timeout and this only ever unwedges the queue behind it.
        "roundTripMs": protocol.RECEIVE_TIMEOUT_MS + 5000,
        "themeCss": str(theme_css or ""),
    }


def document_script(theme_css: str = "", config: typing.Optional[dict] = None) -> str:
    """The whole script, ready for ``add_custom_js``."""
    payload = json.dumps(config if config is not None else configuration(theme_css), sort_keys=True)
    return _SCRIPT.replace("__MINIPAINT_BRIDGE_CONFIG__", payload)


_SCRIPT = r"""
(function () {
  "use strict";

  if (window.__minipaintBridge) { return; }

  var CONFIG = __MINIPAINT_BRIDGE_CONFIG__;

  // The iframe is served from the Forge origin through /wan2gp/, so this page's
  // own origin *is* the expected peer origin. It is read once and never
  // recomputed, and it is what every outbound postMessage targets: "*" would
  // hand every message to whatever happens to be framing us.
  var ORIGIN = window.location.origin;

  var HEX32 = /^[0-9a-f]{32}$/;
  var TOKEN = /^[A-Za-z0-9._:-]{1,64}$/;

  var channelId = "";
  var focusable = Object.create(null);
  var pending = [];
  var busy = false;
  var inFlight = null;
  var watchdog = 0;
  var attempts = 0;
  var timer = 0;
  var encoder = (typeof TextEncoder !== "undefined") ? new TextEncoder() : null;

  function byteLength(text) {
    try { return encoder ? encoder.encode(text).length : String(text).length; }
    catch (error) { return String(text).length; }
  }

  function isHex32(value) { return typeof value === "string" && HEX32.test(value); }
  function isToken(value) { return typeof value === "string" && TOKEN.test(value); }

  function element(id) {
    try { return document.getElementById(id); } catch (error) { return null; }
  }

  function field(id) {
    var host = element(id);
    if (!host) { return null; }
    if (host.tagName === "TEXTAREA" || host.tagName === "INPUT") { return host; }
    return host.querySelector("textarea, input");
  }

  // -- outbound ---------------------------------------------------------------

  function post(kind, requestId, payload) {
    if (!channelId) { return; }
    var envelope = {
      protocol: CONFIG.protocol,
      type: kind,
      channel_id: channelId,
      request_id: String(requestId || ""),
      payload: payload || {}
    };
    try { window.parent.postMessage(envelope, ORIGIN); } catch (error) {}
  }

  // -- the Gradio round trip ---------------------------------------------------

  function submit(request) {
    pending.push(request);
    attempts = 0;
    pump();
  }

  function pump() {
    if (busy || !pending.length) { return; }

    var box = field(CONFIG.requestElemId);
    var button = element(CONFIG.triggerElemId);
    if (!box || !button) {
      // The bridge components have not rendered yet. Bounded retries, then the
      // waiting requests are failed rather than kept forever: a Send that
      // silently never happens is worse than one that says it could not.
      if (attempts >= CONFIG.triggerAttempts) { abandon("IFRAME_NOT_READY"); return; }
      attempts += 1;
      if (!timer) { timer = window.setTimeout(function () { timer = 0; pump(); }, CONFIG.triggerRetryMs); }
      return;
    }

    var next = pending.shift();
    busy = true;
    inFlight = next;
    // Nothing else clears ``busy``. If the Gradio round trip never lands - the
    // server errored, the queue dropped it, WanGP restarted underneath us -
    // then without this every later request would find the queue busy and
    // return without doing anything, and the bridge in this page would be
    // wedged until somebody reloaded it. The parent has its own, shorter,
    // timeout, so in the ordinary case this only tidies up behind it.
    if (watchdog) { window.clearTimeout(watchdog); }
    watchdog = window.setTimeout(function () {
      watchdog = 0;
      if (!busy) { return; }
      busy = false;
      var stranded = inFlight;
      inFlight = null;
      if (stranded) { answer(stranded, { ok: false, code: "RECEIVER_QUERY_TIMEOUT" }); }
      pump();
    }, CONFIG.roundTripMs);
    try {
      box.value = JSON.stringify(next);
      // Gradio binds to the input event; assigning .value alone leaves the
      // server holding the previous string. This is the bridge's own textbox,
      // not a WanGP control - nothing about the receiver is being set here,
      // and the receiver value itself is only ever returned by the callback.
      box.dispatchEvent(new Event("input", { bubbles: true }));
      button.click();
    } catch (error) {
      busy = false;
      inFlight = null;
      if (watchdog) { window.clearTimeout(watchdog); watchdog = 0; }
      answer(next, { ok: false, code: "INTERNAL_ERROR" });
    }
  }

  function abandon(code) {
    var waiting = pending.slice();
    pending.length = 0;
    for (var index = 0; index < waiting.length; index += 1) {
      answer(waiting[index], { ok: false, code: code });
    }
  }

  function replyType(operation) {
    if (operation === "hello") { return CONFIG.types.ready; }
    if (operation === "receivers") { return CONFIG.types.receivers; }
    if (operation === "receive") { return CONFIG.types.receiveResult; }
    return "";
  }

  function answer(request, payload) {
    var kind = replyType(request && request.op);
    if (kind) { post(kind, request.request_id, payload); }
  }

  function remember(payload) {
    focusable = Object.create(null);
    var list = (payload && payload.receivers) || [];
    for (var index = 0; index < list.length; index += 1) {
      var hint = list[index] && list[index].focus_hint;
      if (typeof hint === "string" && hint) { focusable[hint] = true; }
    }
  }

  function deliver(raw) {
    busy = false;
    inFlight = null;
    if (watchdog) { window.clearTimeout(watchdog); watchdog = 0; }
    var payload = null;
    try { payload = JSON.parse(String(raw || "")); } catch (error) { payload = null; }

    if (payload && typeof payload === "object") {
      if (isHex32(payload.channel_id) && payload.channel_id !== channelId) {
        // An acknowledgement for a channel this page is no longer bound to.
        // The parent reloaded the iframe or bound a new one; the old answer is
        // about a session that no longer exists.
        payload = null;
      }
    }

    if (!payload) {
      pump();
      return;
    }

    if (payload.op === "receivers" || payload.op === "hello") { remember(payload); }

    var kind = replyType(payload.op);
    if (kind) { post(kind, payload.request_id, payload); }
    pump();
  }

  // -- inbound ----------------------------------------------------------------

  function readable(event) {
    if (event.origin !== ORIGIN) { return null; }
    if (event.source !== window.parent) { return null; }

    var message = event.data;
    if (!message || typeof message !== "object" || Array.isArray(message)) { return null; }

    var raw;
    try { raw = JSON.stringify(message); } catch (error) { return null; }
    if (!raw || byteLength(raw) >= CONFIG.maxEnvelopeBytes) { return null; }

    if (message.protocol !== CONFIG.protocol) { return null; }
    if (CONFIG.inbound.indexOf(message.type) === -1) { return null; }
    if (!isHex32(message.channel_id)) { return null; }
    if (!isToken(message.request_id)) { return null; }

    var payload = message.payload;
    if (payload === undefined || payload === null) { payload = {}; }
    if (typeof payload !== "object" || Array.isArray(payload)) { return null; }
    message.payload = payload;
    return message;
  }

  function onMessage(event) {
    var message = readable(event);
    if (!message) { return; }

    if (message.type === CONFIG.types.hello) {
      // A hello rebinds the page. Whatever the previous binding believed about
      // receivers belonged to a parent that is no longer talking to us.
      channelId = message.channel_id;
      focusable = Object.create(null);
      pending.length = 0;
      busy = false;
      submit({ op: "hello", request_id: message.request_id, channel_id: channelId });
      return;
    }

    if (!channelId || message.channel_id !== channelId) { return; }

    if (message.type === CONFIG.types.getReceivers) {
      submit({ op: "receivers", request_id: message.request_id, channel_id: channelId });
      return;
    }

    if (message.type === CONFIG.types.receiveImage) {
      var wanted = message.payload;
      if (!isHex32(wanted.handoff_id)) { return; }
      if (CONFIG.receiverIds.indexOf(wanted.receiver_id) === -1) { return; }
      if (typeof wanted.state_revision !== "string" || !wanted.state_revision) { return; }
      submit({
        op: "receive",
        request_id: message.request_id,
        channel_id: channelId,
        handoff_id: wanted.handoff_id,
        receiver_id: wanted.receiver_id,
        state_revision: wanted.state_revision,
        bridge_session: isToken(wanted.bridge_session) ? wanted.bridge_session : "",
        source: (wanted.source && typeof wanted.source === "object" && !Array.isArray(wanted.source)) ? wanted.source : {}
      });
      return;
    }

    if (message.type === CONFIG.types.focusReceiver) {
      focus(message.payload && message.payload.focus_hint);
      return;
    }

    if (message.type === CONFIG.types.themeState) {
      theme(message.payload && message.payload.mode);
      return;
    }
  }

  // -- presentation ------------------------------------------------------------

  function focus(hint) {
    // Only an element id this page published in its own receiver list. Not a
    // selector, not a label, not anything the parent invented.
    if (typeof hint !== "string" || !focusable[hint]) { return; }
    var target = element(hint);
    if (!target || typeof target.scrollIntoView !== "function") { return; }
    try { target.scrollIntoView({ behavior: "smooth", block: "center" }); } catch (error) {}
  }

  function theme(mode) {
    var wanted = (mode === "light") ? "light" : "dark";
    try { document.documentElement.setAttribute("data-minipaint-theme", wanted); } catch (error) {}
  }

  function style() {
    if (!CONFIG.themeCss) { return; }
    try {
      var existing = document.getElementById("minipaint-bridge-theme");
      if (existing) { return; }
      var tag = document.createElement("style");
      tag.id = "minipaint-bridge-theme";
      tag.textContent = CONFIG.themeCss;
      (document.head || document.documentElement).appendChild(tag);
    } catch (error) {
      // Presentation only. Section 27.1: a theme that will not apply must not
      // stop an image from being handed over.
    }
  }

  window.__minipaintBridge = { deliver: deliver };

  theme("dark");
  style();
  window.addEventListener("message", onMessage, false);
})();
"""
