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

#: How often the acknowledgement box is read while a request is in flight -
#: the second route for an answer whose chained ``.then(js=...)`` never ran.
ACK_POLL_MS = 300

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
        # Classes, not ids: WanGP builds its form twice and the bridge places
        # one set of controls in each, so the script looks for "the set that
        # is on screen" rather than for one element.
        "columnClass": bridge_ui.COLUMN_CLASS,
        "requestClass": bridge_ui.REQUEST_CLASS,
        "ackClass": bridge_ui.ACK_CLASS,
        "triggerClass": bridge_ui.TRIGGER_CLASS,
        "triggerAttempts": TRIGGER_ATTEMPTS,
        "triggerRetryMs": TRIGGER_RETRY_MS,
        "ackPollMs": ACK_POLL_MS,
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
  // The request in flight, as the operator would want it described: when it
  // was clicked, in which set of controls, and what the acknowledgement box
  // held before - so a failure can say more than "nothing came back".
  var flight = null;
  var poll = 0;

  function log(message) {
    try { if (typeof console !== "undefined" && console.debug) { console.debug("[minipaint bridge] " + message); } }
    catch (error) {}
  }
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

  // -- finding our own controls -------------------------------------------------
  //
  // WanGP builds its generator form twice - once for the Media Generator tab
  // and once for the hidden Edit tab - and the bridge places one set of
  // controls in each, so nothing here is looked up by id. The live set is the
  // one whose surroundings are displayed: the Edit tab's copy is wired to the
  // Edit form's values, which are not the ones on screen. The set's own column
  // is hidden by design, so the walk starts above it.

  function columns() {
    try { return Array.prototype.slice.call(document.getElementsByClassName(CONFIG.columnClass)); }
    catch (error) { return []; }
  }

  function surroundingsDisplayed(column) {
    var node = column.parentElement;
    while (node && node !== document.body) {
      try { if (window.getComputedStyle(node).display === "none") { return false; } }
      catch (error) { return true; }
      node = node.parentElement;
    }
    return true;
  }

  function liveColumn() {
    var found = columns();
    for (var index = 0; index < found.length; index += 1) {
      if (surroundingsDisplayed(found[index])) { return found[index]; }
    }
    // Every set is inside something hidden - a form whose image row is folded
    // away for the current model, say. The first one built is the Media
    // Generator's, and that is the form the user is looking at.
    return found.length ? found[0] : null;
  }

  function controlIn(column, className) {
    if (!column) { return null; }
    try { return column.querySelector("." + className); } catch (error) { return null; }
  }

  function fieldIn(column, className) {
    var host = controlIn(column, className);
    if (!host) { return null; }
    if (host.tagName === "TEXTAREA" || host.tagName === "INPUT") { return host; }
    return host.querySelector("textarea, input");
  }

  function buttonIn(column, className) {
    var host = controlIn(column, className);
    if (!host) { return null; }
    if (host.tagName === "BUTTON") { return host; }
    return host.querySelector("button");
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

    var column = liveColumn();
    var box = fieldIn(column, CONFIG.requestClass);
    var button = buttonIn(column, CONFIG.triggerClass);
    if (!box || !button) {
      // The bridge components have not rendered yet. Bounded retries, then the
      // waiting requests are failed rather than kept forever: a Send that
      // silently never happens is worse than one that says it could not.
      if (attempts >= CONFIG.triggerAttempts) {
        var missing = "the bridge controls were not found on this page after " + attempts + " attempts ("
          + columns().length + " column(s) present)";
        log(missing);
        abandon("IFRAME_NOT_READY", missing);
        return;
      }
      attempts += 1;
      if (!timer) { timer = window.setTimeout(function () { timer = 0; pump(); }, CONFIG.triggerRetryMs); }
      return;
    }

    var next = pending.shift();
    busy = true;
    inFlight = next;
    var ackField = fieldIn(column, CONFIG.ackClass);
    flight = {
      request: next,
      clickedAt: Date.now(),
      set: (column && column.id) || "(no id)",
      ackBefore: ackField ? String(ackField.value || "") : ""
    };
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
      var record = flight;
      inFlight = null;
      flight = null;
      stopPoll();
      if (stranded) {
        var detail = "no acknowledgement " + (record ? (Date.now() - record.clickedAt) : 0) + " ms after the click on "
          + (record ? record.set : "(no set)") + " - the Gradio event never completed, or its result never reached this page";
        log(stranded.op + ": " + detail);
        answer(stranded, { ok: false, code: "RECEIVER_QUERY_TIMEOUT", detail: detail });
      }
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
      log(next.op + ": clicked on " + flight.set);
      startPoll(ackField);
    } catch (error) {
      busy = false;
      inFlight = null;
      flight = null;
      stopPoll();
      if (watchdog) { window.clearTimeout(watchdog); watchdog = 0; }
      answer(next, { ok: false, code: "INTERNAL_ERROR", detail: "the click could not be dispatched: " + String(error && error.message || error).slice(0, 120) });
    }
  }

  // The acknowledgement arrives through Gradio's own ``.then(js=...)`` on the
  // event, and that is the mechanism that has to be right. This is the second
  // route for the case where the event completed and wrote the box but the
  // chained call never ran: the box is read while a request is in flight, and
  // a value that changed and names this request is delivered the same way.
  // Bounded by the watchdog, never runs while nothing is in flight.

  function startPoll(ackField) {
    stopPoll();
    if (!ackField) { return; }
    poll = window.setInterval(function () {
      if (!busy || !flight) { stopPoll(); return; }
      var now = String(ackField.value || "");
      if (!now || now === flight.ackBefore) { return; }
      var seen = null;
      try { seen = JSON.parse(now); } catch (error) { seen = null; }
      if (!seen || seen.request_id !== flight.request.request_id) { return; }
      log(flight.request.op + ": acknowledgement read from the box (the chained call had not delivered it)");
      deliver(now);
    }, CONFIG.ackPollMs);
  }

  function stopPoll() {
    if (poll) { window.clearInterval(poll); poll = 0; }
  }

  function abandon(code, detail) {
    var waiting = pending.slice();
    pending.length = 0;
    for (var index = 0; index < waiting.length; index += 1) {
      answer(waiting[index], { ok: false, code: code, detail: detail || "" });
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
    var record = flight;
    flight = null;
    stopPoll();
    if (watchdog) { window.clearTimeout(watchdog); watchdog = 0; }
    if (record) { log(record.request.op + ": acknowledged after " + (Date.now() - record.clickedAt) + " ms"); }
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
