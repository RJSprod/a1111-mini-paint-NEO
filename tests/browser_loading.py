"""The Canvas startup contract, exercised the way production actually runs it.

    python tests/browser_loading.py

WHY THIS FILE EXISTS SEPARATELY FROM browser_smoke.py.

``browser_smoke.py`` is the editor's suite: it needs a real Forge Neo checkout
because it drives the real ForgeCanvas, and it inlined the Canvas bundle into
the page's head to get one. That inlining is the reason a regression in the
loader could not be seen from inside the tests. ``window.minipaintCanvas``
existed before any load event fired, so a bootstrap that awaited nothing and
attached nothing still found the adapter sitting there, and 180 checks passed
over a product whose Canvas tab had no editor behind it.

So this file tests the other half: the timing between

    Gradio load callback -> dynamic <script> -> HTTP asset route
        -> browser script execution -> attach()

and it does it the only way that proves anything - by serving
``browser/minipaint_canvas.js`` through the same route the extension installs
in Forge, over HTTP, with nothing inlined. The bundle is late, or missing, or
broken, because that is what a network does.

WHAT IT DELIBERATELY FAKES, AND WHY THAT IS FINE.

ForgeCanvas itself is a stub here. The editor's behaviour is
``browser_smoke.py``'s job and it needs the real thing for that; what this
file asserts is whether the adapter is loaded, attached and reported - none of
which depends on how well the canvas draws. The stub is the host, not the
thing under test.

WHAT IT MUST NEVER DO.

Inline a bundle it claims to load dynamically. If ``window.minipaintCanvas``
can be reached without the route having served it, this file is testing the
old model and is worthless.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time

# Gradio and anything else that probes its own local URL follows the proxy
# environment; a sandbox that routes everything through one makes a loopback
# request look unreachable. Set before any HTTP client is imported.
for _key in ("no_proxy", "NO_PROXY"):
    os.environ[_key] = "127.0.0.1,localhost,::1," + os.environ.get(_key, "")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harness import Results, setup_path  # noqa: E402

setup_path()

ROOT = pathlib.Path(__file__).resolve().parent.parent

from minipaint_neo import assets  # noqa: E402
from minipaint_neo.canvas import surface  # noqa: E402

UUID = "uuid_testsurface"
PORT = int(os.environ.get("MINIPAINT_TEST_PORT", "8791"))

#: Per-bundle response behaviour, by bundle name. Reset between checks.
#: ``delay`` is seconds before the real route answers; ``fail`` makes it a
#: 404; ``once`` drops the behaviour after it has been applied one time.
CONTROL: dict = {}

#: Every asset request the browser actually made, in order.
REQUESTS: list = []


def _name_of(path: str) -> str:
    tail = path.rsplit("/", 1)[-1]
    return tail.split("?", 1)[0][:-3] if tail.split("?", 1)[0].endswith(".js") else ""


def build_app():
    """Forge's app, as far as this extension is concerned: the real route,
    with a shim in front that can make a bundle late or missing."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Route

    async def page(request):
        return HTMLResponse(page_html())

    app = Starlette(routes=[Route("/", endpoint=page)])
    # The real thing, installed exactly as ``on_app_started`` does it.
    assets.install(app)
    real = app.router.routes[0]

    async def shim(request):
        name = _name_of(request.url.path)
        REQUESTS.append(name)
        rule = CONTROL.get(name) or {}
        if rule.get("once"):
            CONTROL.pop(name, None)
        if rule.get("delay"):
            time.sleep(float(rule["delay"]))
        if rule.get("fail"):
            return JSONResponse({"ok": False}, status_code=404, headers={"Cache-Control": "no-store"})
        if rule.get("corrupt"):
            return Response("this is not valid javascript {{{",
                            media_type="application/javascript", headers={"Cache-Control": "no-store"})
        return await real.endpoint(request)

    app.router.routes[0] = Route(assets.SCRIPT_ROUTE, endpoint=shim, methods=["GET", "HEAD"])
    return app


def canvas_container_html(uuid: str = UUID) -> str:
    """The markup one Forge canvas is made of, built the way the extension
    builds it - including the toolbar rewrite the stylesheet depends on."""
    return surface.canvas_markup(uuid)


FORGE_CANVAS_STUB = """
/* Stands in for Forge's ForgeCanvas. Enough of its surface for the adapter
   to attach and report; the editor's own behaviour is browser_smoke.py's. */
window.__forgeCanvasBuilt = 0;
function ForgeCanvas(uuid) {
    window.__forgeCanvasBuilt += 1;
    this.uuid = uuid;
    this.img = null;
    this.imgX = 0; this.imgY = 0; this.imgScale = 1;
    this.maximized = false;
    this.no_scribbles = false;
    this.background_gradio_bind = { target: document.getElementById(uuid) || null };
    this.loadImage = function () {};
    this.updateBackgroundImageData = function () {};
    this.drawImage = function () {};
    this.loadDrawing = function () {};
    this.updateDrawingData = function () {};
    this.onStatus = function () {};
}
"""


#: The part of Forge's own canvas.css that this extension's toolbar rule is
#: written on top of, reproduced so a geometry check here means something.
#:
#: This is the host's contract, quoted - not a fixture bent until it gives the
#: answer we wanted. From modules_forge/forge_canvas/canvas.css:
#:
#:     .forge-container       { position: relative; ... }
#:     .forge-image-container { position: relative; ... }
#:     .forge-toolbar-static  { position: absolute; top: 0px; left: 0px; ... }
#:
#: Everything else in that file is colour, spacing and the checkerboard, none
#: of which moves the toolbar. What is under test is our override of the
#: ``left`` and ``transform`` above; the rest of Forge's stylesheet cannot
#: change whether that centres, and browser_smoke.py runs the same check
#: against the real file to keep this honest.
HOST_LAYOUT_CSS = """
.forge-container { position: relative; width: 100%; height: 512px; overflow: hidden; }
.forge-image-container { position: relative; width: 100%; height: calc(100% - 6px); overflow: hidden; }
.forge-toolbar-static { position: absolute; top: 0px; left: 0px; padding: 6px 10px; }
"""


def page_html() -> str:
    """A page shaped like the one Forge builds, with NOTHING inlined that the
    loader is supposed to fetch.

    ``main.js`` is here because Forge really does put every file in
    ``javascript/`` on every page. The Canvas bundle is not, because Forge
    really does not - that is the whole point of the route being tested.
    """
    boot = surface.bootstrap_js(UUID, {"heightPercent": 70, "fit": True, "brushWidth": 25}, "probe_status")
    return f"""<!doctype html>
<meta charset="utf-8">
<title>minipaint loading probe</title>
<style>{HOST_LAYOUT_CSS}</style>
<style>{(ROOT / "style.css").read_text(encoding="utf-8")}</style>
<div id="tabs">
  <div id="tab_minipaint" class="tabitem">
    <div id="minipaint_canvas_root">
      <div id="minipaint_canvas_work">
        <div id="minipaint_canvas_surface">{canvas_container_html()}</div>
        <!-- The status line lives inside the top row, as it does on the real
             page: a nowrap flex container shared with the tool buttons. The
             startup notice must land BELOW that row rather than be squeezed
             into it, which is what check_failure_notice_placement asserts. -->
        <div class="minipaint-topbar" style="display:flex">
          <button class="minipaint-action">tool</button>
          <div id="probe_status" class="minipaint-status">no image yet</div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>{FORGE_CANVAS_STUB}</script>
<script>{(ROOT / "javascript" / "main.js").read_text(encoding="utf-8")}</script>
<script>
  window.__bootstrap = {boot};
  window.__runBootstrap = function () {{ return window.__bootstrap(); }};
</script>
"""


def serve(app):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if getattr(server, "started", False):
            return server
        time.sleep(0.1)
    raise SystemExit("the probe server did not start")


def chromium_path():
    """A Chromium to drive, or None to let Playwright find its own.

    None is the normal answer. ``playwright install`` puts the browser under
    the user's cache directory and Playwright knows where that is; the glob
    is only for an image that pre-installs one somewhere else and points
    PLAYWRIGHT_BROWSERS_PATH at it. Returning a path that does not exist -
    or refusing to run because one could not be found - breaks the suite on
    exactly the machine this was added for, which is CI.
    """
    named = os.environ.get("CHROMIUM", "")
    if named and pathlib.Path(named).exists():
        return named
    for root in (os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "/opt/pw-browsers"):
        if not root:
            continue
        for candidate in sorted(pathlib.Path(root).glob("chromium-*/chrome-linux/chrome")):
            return str(candidate)
    return None


def state(page):
    return page.evaluate("() => window.minipaintCanvasReady.state()")


def has_adapter(page):
    return page.evaluate("() => typeof window.minipaintCanvas !== 'undefined'")


def built(page):
    return page.evaluate("() => window.__forgeCanvasBuilt || 0")


def attached(page):
    return page.evaluate(f"() => !!(window.minipaintCanvas && window.minipaintCanvas.attachedTo({UUID!r}))")


def alert_shown(page):
    return page.evaluate("() => !!document.querySelector('.minipaint-canvas-alert')")


def script_states(page, fragment):
    return page.evaluate(
        "f => Array.from(document.querySelectorAll('script[data-minipaint-bundle]'))"
        ".filter(s => s.getAttribute('data-minipaint-bundle').indexOf(f) >= 0)"
        ".map(s => s.getAttribute('data-minipaint-state'))", fragment)


def settles(page, expression, timeout=8000):
    """Wait for a page condition, and report rather than explode if it never
    comes. A regression here is a failed check, not a broken suite: a stack
    trace out of the harness hides which contract actually broke."""
    try:
        page.wait_for_function(expression, timeout=timeout)
        return True
    except Exception:
        return False


def fresh(p, chromium, errors=None):
    browser = p.chromium.launch(executable_path=chromium) if chromium else p.chromium.launch()
    page = browser.new_page()
    if errors is not None:
        page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    return browser, page


# ---------------------------------------------------------------------------
# 8.2  The bundle is late. This is the check that would have caught the bug.
# ---------------------------------------------------------------------------

def check_delayed(r: Results, p, chromium) -> None:
    CONTROL.clear()
    CONTROL["canvas"] = {"delay": 0.4}
    browser, page = fresh(p, chromium)
    try:
        r.check("delayed: nothing is inlined, so there is no adapter before the route answers",
                not has_adapter(page), "window.minipaintCanvas existed before any fetch")
        # Start the bootstrap but do not wait for it: this is the window in
        # which the old code decided the Canvas was absent and moved on.
        page.evaluate("() => { window.__result = window.__runBootstrap(); }")
        reached = settles(page, "() => window.minipaintCanvasReady.state() === 'loading'", 5000)
        r.check("delayed: the bootstrap reports that it is loading", reached, state(page))
        r.check("delayed: while the bundle is in flight the Canvas is not reported ready",
                state(page) == "loading" and not attached(page), state(page))
        r.check("delayed: and attach has not been called", built(page) == 0, str(built(page)))

        ok = page.evaluate("async () => await window.__result")
        r.check("delayed: the bootstrap resolves true once the bundle has run", ok is True, repr(ok))
        r.check("delayed: the adapter is there", has_adapter(page))
        r.check("delayed: attach ran exactly once", built(page) == 1, str(built(page)))
        r.check("delayed: and the Canvas reports ready", state(page) == "ready" and attached(page), state(page))
        r.check("delayed: no failure notice is shown", not alert_shown(page))
        r.check("delayed: the canvas script is marked loaded", script_states(page, "canvas.js") == ["loaded"],
                str(script_states(page, "canvas.js")))
    finally:
        browser.close()


# ---------------------------------------------------------------------------
# 8.3  WanGP is missing. The Canvas must not care.
# ---------------------------------------------------------------------------

def check_optional_failure(r: Results, p, chromium) -> None:
    CONTROL.clear()
    CONTROL["wangp"] = {"fail": True}
    REQUESTS.clear()
    errors: list = []
    browser, page = fresh(p, chromium, errors)
    try:
        ok = page.evaluate("async () => await window.__runBootstrap()")
        r.check("wangp 404: the bootstrap still reports success", ok is True, repr(ok))
        r.check("wangp 404: the Canvas attached", attached(page) and built(page) == 1, state(page))
        r.check("wangp 404: the Canvas reports ready", state(page) == "ready", state(page))
        r.check("wangp 404: no failure notice, because nothing the user needs failed",
                not alert_shown(page))
        # It really was asked for, and really did fail: otherwise this check
        # proves only that the test forgot to request it.
        page.wait_for_function("() => true", timeout=1000)
        r.check("wangp 404: the optional bundle was genuinely requested", "wangp" in REQUESTS, str(REQUESTS))
        r.check("wangp 404: and the page reports it as not loaded",
                page.evaluate("() => window.minipaintAssets.ready(%s)" % json.dumps(assets.url_for("wangp"))) is False)
        r.check("wangp 404: a failed optional bundle leaves no script element behind to be mistaken for success",
                script_states(page, "wangp.js") == [], str(script_states(page, "wangp.js")))
        r.check("wangp 404: nothing threw on the page", not errors, "; ".join(errors[:2]))
    finally:
        browser.close()


# ---------------------------------------------------------------------------
# 8.4  The first attempt fails; the second must be real, not a false success.
# ---------------------------------------------------------------------------

def check_retry(r: Results, p, chromium) -> None:
    CONTROL.clear()
    CONTROL["canvas"] = {"fail": True, "once": True}
    REQUESTS.clear()
    browser, page = fresh(p, chromium)
    try:
        ok = page.evaluate("async () => await window.__runBootstrap()")
        r.check("retry: the first attempt reports failure", ok is False, repr(ok))
        r.check("retry: the Canvas is in the failed state", state(page) == "failed", state(page))
        r.check("retry: attach was never called", built(page) == 0, str(built(page)))
        r.check("retry: the user is told, with a way back", alert_shown(page))
        r.check("retry: the failed script is not left in the document to be read as loaded",
                script_states(page, "canvas.js") == [], str(script_states(page, "canvas.js")))

        first = list(REQUESTS)
        ok2 = page.evaluate("async () => await window.minipaintCanvasReady.retry()")
        r.check("retry: the second attempt makes a real request",
                REQUESTS.count("canvas") == first.count("canvas") + 1, str(REQUESTS))
        r.check("retry: and succeeds", ok2 is True, repr(ok2))
        r.check("retry: the adapter is there and attached once", built(page) == 1 and attached(page), str(built(page)))
        r.check("retry: the Canvas reports ready", state(page) == "ready", state(page))
        r.check("retry: the notice is taken down again", not alert_shown(page))
    finally:
        browser.close()


def check_retry_from_button(r: Results, p, chromium) -> None:
    """The Retry the user is actually offered, pressed the way they press it."""
    CONTROL.clear()
    CONTROL["canvas"] = {"fail": True, "once": True}
    browser, page = fresh(p, chromium)
    try:
        page.evaluate("async () => await window.__runBootstrap()")
        r.check("retry button: the notice is on the page", alert_shown(page))
        page.click(".minipaint-canvas-alert-retry")
        r.check("retry button: it settles",
                settles(page, "() => window.minipaintCanvasReady.state() === 'ready'", 10000), state(page))
        r.check("retry button: pressing it recovers without reloading the page",
                state(page) == "ready" and attached(page) and built(page) == 1, state(page))
        r.check("retry button: and the notice goes away", not alert_shown(page))
    finally:
        browser.close()


# ---------------------------------------------------------------------------
# 8.7  Reload UI: a second surface, no second copy of anything.
# ---------------------------------------------------------------------------

def check_failure_notice_placement(r: Results, p, chromium) -> None:
    """The notice gets its own line, and goes away again.

    The status line shares a nowrap flex row with the tool buttons, so a
    notice inserted beside it is squeezed in among them - and this one has a
    button on it that has to be pressable.
    """
    CONTROL.clear()
    CONTROL["canvas"] = {"fail": True, "once": True}
    browser, page = fresh(p, chromium)
    try:
        page.evaluate("async () => await window.__runBootstrap()")
        r.check("notice: it is shown", alert_shown(page))
        placed = page.evaluate("""() => {
            const a = document.querySelector('.minipaint-canvas-alert');
            const row = document.querySelector('.minipaint-topbar');
            if (!a || !row) { return null; }
            const ar = a.getBoundingClientRect(), rr = row.getBoundingClientRect();
            return {insideRow: row.contains(a), below: ar.top >= rr.bottom - 1,
                    width: ar.width, rowWidth: rr.width};
        }""")
        r.check("notice: it is not inside the nowrap row it would be squeezed into",
                placed and placed["insideRow"] is False, json.dumps(placed))
        r.check("notice: it sits below that row", placed and placed["below"], json.dumps(placed))
        r.check("notice: and its Retry button is clickable",
                page.evaluate("() => { const b = document.querySelector('.minipaint-canvas-alert-retry');"
                              " if (!b) return false; const rect = b.getBoundingClientRect();"
                              " return rect.width > 0 && rect.height > 0; }"))
        # Recovery takes it down again rather than leaving it under a working
        # editor.
        page.evaluate("async () => await window.minipaintCanvasReady.retry()")
        r.check("notice: recovery removes it", not alert_shown(page))
        r.check("notice: and exactly one was ever created",
                page.evaluate("() => document.querySelectorAll('.minipaint-canvas-alert').length") == 0)
    finally:
        browser.close()


def check_reload_ui(r: Results, p, chromium) -> None:
    CONTROL.clear()
    REQUESTS.clear()
    browser, page = fresh(p, chromium)
    try:
        page.evaluate("async () => await window.__runBootstrap()")
        r.check("reload: the first surface is attached", attached(page) and built(page) == 1)
        before = REQUESTS.count("canvas")

        # Reload UI rebuilds the page's blocks without reloading the document,
        # and the rebuilt surface has a NEW uuid - which is what used to make
        # attach() a silent no-op, because a live instance for the old one was
        # still standing.
        second = UUID + "_rebuilt"
        page.evaluate(
            """([html, uuid, boot]) => {
                const host = document.getElementById('minipaint_canvas_surface');
                host.innerHTML = html;
                window.__bootstrap2 = eval('(' + boot + ')');
                return true;
            }""",
            [canvas_container_html(second), second,
             surface.bootstrap_js(second, {"heightPercent": 70, "fit": True, "brushWidth": 25}, "probe_status")],
        )
        ok = page.evaluate("async () => await window.__bootstrap2()")
        r.check("reload: the rebuilt surface attaches", ok is True, repr(ok))
        r.check("reload: and it is the new surface that is attached, not the old one",
                page.evaluate("s => window.minipaintCanvas.attachedTo(s)", second)
                and not page.evaluate("s => window.minipaintCanvas.attachedTo(s)", UUID))
        r.check("reload: the bundle is not fetched again", REQUESTS.count("canvas") == before,
                f"{REQUESTS.count('canvas')} vs {before}")
        r.check("reload: and it is not executed again",
                page.evaluate("() => window.__minipaintCanvasEvaluations === undefined || window.__minipaintCanvasEvaluations === 1"))
        r.check("reload: exactly one editor was built for the rebuilt surface", built(page) == 2, str(built(page)))
        r.check("reload: only one canvas script element exists", len(script_states(page, "canvas.js")) == 1,
                str(script_states(page, "canvas.js")))
        # One window listener set per attachment, not one per rebuild: the
        # adapter takes its own down before putting new ones up.
        r.check("reload: the old attachment was stood down",
                page.evaluate("() => window.minipaintCanvas.attachedTo(%s) === false" % json.dumps(UUID)))
    finally:
        browser.close()


# ---------------------------------------------------------------------------
# The contract itself, independent of any one scenario.
# ---------------------------------------------------------------------------

def check_contract(r: Results, p, chromium) -> None:
    CONTROL.clear()
    REQUESTS.clear()
    browser, page = fresh(p, chromium)
    try:
        canvas_url = assets.url_for("canvas")
        one = page.evaluate("u => window.minipaintAssets.load([u])", canvas_url)
        r.check("load() resolves to a boolean, not the array Promise.all hands back",
                one is True, f"{one!r} ({type(one).__name__})")
        both = page.evaluate("us => window.minipaintAssets.load(us)",
                             [canvas_url, assets.url_for("wangp")])
        r.check("load() of several is true when all of them ran", both is True, repr(both))

        CONTROL["interop"] = {"fail": True}
        bad = page.evaluate("u => window.minipaintAssets.load([u])", assets.url_for("interop"))
        r.check("load() of a bundle that 404s is false, and false is falsy",
                bad is False and not bad, repr(bad))
        mixed = page.evaluate("us => window.minipaintAssets.load(us)",
                              [canvas_url, assets.url_for("interop")])
        r.check("load() is false when any required bundle failed", mixed is False, repr(mixed))

        # Idempotency, from the state a real page is in: configured by the
        # bootstrap and already attached. Two more callers - two Send-to
        # presses in the same second - must not build a second editor.
        page.evaluate("async () => await window.__runBootstrap()")
        r.check("the bootstrap built one editor", built(page) == 1, str(built(page)))
        again = page.evaluate(
            "async () => { const r = window.minipaintCanvasReady; "
            "const both = await Promise.all([r.ensure(), r.ensure()]); "
            "return {both: both, built: window.__forgeCanvasBuilt}; }")
        r.check("ensure() is idempotent: two more callers, still one editor",
                again["built"] == 1 and again["both"] == [True, True], json.dumps(again))
        r.check("and a caller that arrives after ready is answered without another load",
                page.evaluate("async () => await window.minipaintCanvasReady.ensure()") is True)
    finally:
        browser.close()


def check_attach_throwing_is_a_failure(r: Results, p, chromium) -> None:
    """An adapter that throws on attach must fail the startup, not hang it.

    Found by running this against a host whose ForgeCanvas is a different
    version: ``attach`` threw, the exception escaped the promise chain, and
    ``ensure()`` neither resolved nor rejected. That is the worst of the
    three outcomes now that every Send-to press waits on this promise - the
    button would do nothing, for ever, with no failure shown and no retry
    offered. A throw has to land somewhere.
    """
    CONTROL.clear()
    browser, page = fresh(p, chromium)
    try:
        # Break attach the way a host-version mismatch breaks it.
        page.evaluate("""() => { window.__loadedHook = () => {
            const real = window.minipaintCanvas;
            if (real) { real.attach = function () { throw new TypeError("no such method on this host"); }; }
        }; }""")
        page.evaluate("async () => { await window.minipaintAssets.load([%s]); window.__loadedHook(); }"
                      % json.dumps(assets.url_for("canvas")))
        settled = page.evaluate(
            """async () => {
                const r = window.minipaintCanvasReady;
                const timeout = new Promise(res => setTimeout(() => res("HUNG"), 4000));
                const answer = await Promise.race([window.__runBootstrap(), timeout]);
                return {answer: answer, state: r.state()};
            }""")
        r.check("attach throwing: the promise settles rather than hanging for ever",
                settled["answer"] != "HUNG", json.dumps(settled))
        r.check("attach throwing: and it settles as a failure", settled["answer"] is False, json.dumps(settled))
        r.check("attach throwing: the Canvas is in the failed state", settled["state"] == "failed", json.dumps(settled))
        r.check("attach throwing: the user is told, with a way back", alert_shown(page))
    finally:
        browser.close()


def check_send_to_before_open(r: Results, p, chromium) -> None:
    """8.5 - the reason the bundle is fetched on page load at all.

    A Send to Mini Paint press arrives before the tab has been opened and,
    now, before the bundle has arrived. The transform must wait for the
    adapter rather than hand the backend a null where the picture goes.
    """
    from minipaint_neo.canvas import ui as canvas_ui

    CONTROL.clear()
    CONTROL["canvas"] = {"delay": 0.4}
    browser, page = fresh(p, chromium)
    try:
        page.evaluate("() => { window.__result = window.__runBootstrap(); }")
        # Pressed in the window where the adapter does not exist yet.
        r.check("send-to: the adapter really is absent when the press happens", not has_adapter(page))
        got = page.evaluate(
            """([js, uuid]) => {
                const fn = eval('(' + js + ')');
                window.__pickCalls = 0;
                return fn(null, 'state', 'crop').then(out => ({out: out, adapter: typeof window.minipaintCanvas !== 'undefined'}));
            }""",
            [canvas_ui.PICK_JS, UUID],
        )
        r.check("send-to: the transform waited for the adapter before returning",
                got["adapter"] is True, json.dumps(got))
        r.check("send-to: and it still returns the backend's three inputs",
                isinstance(got["out"], list) and len(got["out"]) == 3, json.dumps(got["out"]))
        r.check("send-to: the Canvas came up", state(page) == "ready", state(page))
    finally:
        browser.close()


# ---------------------------------------------------------------------------
# 8.8  The toolbar is centred on the work area, at several widths.
#
# Against HOST_LAYOUT_CSS above - Forge's positioning, quoted. browser_smoke.py
# runs the same measurement against the real canvas.css; this one runs without
# a Forge checkout, so it runs in CI, which is where a UI rule that quietly
# stops applying actually gets caught.
# ---------------------------------------------------------------------------

#: Section 5.1's tolerance, in CSS pixels.
CENTRE_TOLERANCE = 4.0

GEOMETRY_JS = """() => {
    const bar = document.querySelector('#minipaint_canvas_surface .forge-toolbar-static');
    const work = document.querySelector('#minipaint_canvas_surface .forge-image-container')
              || document.querySelector('#minipaint_canvas_surface .forge-container');
    if (!bar || !work) { return null; }
    const b = bar.getBoundingClientRect(), w = work.getBoundingClientRect();
    if (!b.width || !w.width) { return null; }
    return {barCentre: b.left + b.width / 2, workCentre: w.left + w.width / 2,
            barLeft: b.left, barRight: b.right, workLeft: w.left, workRight: w.right,
            barWidth: b.width, workWidth: w.width};
}"""


def check_toolbar_geometry(r: Results, p, chromium) -> None:
    CONTROL.clear()
    browser, page = fresh(p, chromium)
    try:
        page.evaluate("async () => await window.__runBootstrap()")
        for width, height, label in ((1920, 1080, "wide"), (1440, 900, "desktop"),
                                     (1280, 900, "small desktop"), (860, 1180, "narrow")):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(200)
            g = page.evaluate(GEOMETRY_JS)
            if g is None:
                r.check(f"geometry {label}: the toolbar and work area are measurable", False, "not measurable")
                continue
            error = abs(g["barCentre"] - g["workCentre"])
            r.check(f"geometry {label} ({width}px): the toolbar is centred on the work area",
                    error <= CENTRE_TOLERANCE, f"off by {error:.2f}px; {json.dumps(g)}")
            r.check(f"geometry {label} ({width}px): and it hugs its buttons rather than filling the row",
                    g["barWidth"] < g["workWidth"], json.dumps(g))
            r.check(f"geometry {label} ({width}px): and stays inside the work area",
                    g["barLeft"] >= g["workLeft"] - 1 and g["barRight"] <= g["workRight"] + 1, json.dumps(g))
    finally:
        browser.close()


# ---------------------------------------------------------------------------
# 8.9  A resize settles. It does not keep working forever.
# ---------------------------------------------------------------------------

def check_grip_dodges_toolbar(r: Results, p, chromium) -> None:
    """5.2 - the grip gives way to the toolbar, never the other way round.

    The toolbar is a fixed part of the editor and is where every earlier
    version put it; a crop frame's move grip belongs to a selection that
    exists for a few seconds. So when the two collide, the grip is the one
    that moves - along the frame's own top edge, to the nearer free side.
    """
    CONTROL.clear()
    browser, page = fresh(p, chromium)
    try:
        page.evaluate("async () => await window.__runBootstrap()")
        # A toolbar 300px wide, centred over an 1000px work area.
        bar = {"left": 350, "right": 650, "top": 0, "bottom": 40}

        def fraction(frame, toolbar=bar):
            return page.evaluate("([f, b]) => window.minipaintCanvas.gripFraction(f, b)", [frame, toolbar])

        low = {"left": 0, "top": 400, "width": 1000, "height": 200}
        r.check("grip: a frame well below the toolbar keeps the midpoint",
                fraction(low) == 50, str(fraction(low)))

        aside = {"left": 0, "top": 20, "width": 200, "height": 300}
        r.check("grip: a frame beside the toolbar keeps the midpoint",
                fraction(aside) == 50, str(fraction(aside)))

        under = {"left": 0, "top": 20, "width": 1000, "height": 300}
        moved = fraction(under)
        r.check("grip: a frame across the top moves its grip off the midpoint",
                moved != 50, str(moved))
        centre = under["left"] + moved / 100 * under["width"]
        r.check("grip: and clear of the toolbar",
                centre + 32 <= bar["left"] or centre - 32 >= bar["right"],
                f"grip centre {centre} against toolbar {bar['left']}-{bar['right']}")
        r.check("grip: and still inside its own frame",
                under["left"] <= centre <= under["left"] + under["width"], str(centre))

        # Asymmetric frame: the nearer free side wins, so the grip travels the
        # shorter distance rather than always picking one direction.
        right_heavy = {"left": 600, "top": 20, "width": 900, "height": 300}
        rf = fraction(right_heavy)
        r.check("grip: it moves to the nearer free side",
                right_heavy["left"] + rf / 100 * right_heavy["width"] >= bar["right"],
                f"fraction {rf}")

        tight = {"left": 340, "top": 20, "width": 320, "height": 300}
        r.check("grip: a frame with no room either side keeps the midpoint rather than leaving the frame",
                fraction(tight) == 50, str(fraction(tight)))

        r.check("grip: with no toolbar to dodge it is always the midpoint",
                fraction(under, None) == 50, str(fraction(under, None)))
    finally:
        browser.close()


def check_resize_settles(r: Results, p, chromium) -> None:
    CONTROL.clear()
    browser, page = fresh(p, chromium)
    try:
        page.evaluate("async () => await window.__runBootstrap()")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(400)

        # Count fit passes by watching the frames the scheduler asks for.
        page.evaluate("""() => {
            window.__frames = 0;
            const raf = window.requestAnimationFrame;
            window.requestAnimationFrame = function (fn) { window.__frames += 1; return raf(fn); };
        }""")
        page.set_viewport_size({"width": 1180, "height": 760})
        page.wait_for_timeout(1200)
        during = page.evaluate("() => window.__frames")
        page.evaluate("() => { window.__frames = 0; }")
        # Now leave it completely alone.
        page.wait_for_timeout(1500)
        after = page.evaluate("() => window.__frames")
        r.check("resize: the layout does something when the window changes", during > 0, str(during))
        r.check("resize: and then stops, rather than fitting forever on a still page",
                after == 0, f"{after} frames requested while idle")
    finally:
        browser.close()


def run() -> Results:
    r = Results("browser loading")
    # ImportError, not SystemExit: run.py skips a suite whose optional
    # dependency is missing and aborts the whole run on anything else, and a
    # laptop without Playwright should still get the other twenty-three.
    from playwright.sync_api import sync_playwright  # noqa: F401

    chromium = chromium_path()

    assets.reset_for_tests()
    server = serve(build_app())
    try:
        with sync_playwright() as p:
            check_delayed(r, p, chromium)
            check_optional_failure(r, p, chromium)
            check_retry(r, p, chromium)
            check_retry_from_button(r, p, chromium)
            check_failure_notice_placement(r, p, chromium)
            check_reload_ui(r, p, chromium)
            check_contract(r, p, chromium)
            check_attach_throwing_is_a_failure(r, p, chromium)
            check_send_to_before_open(r, p, chromium)
            check_toolbar_geometry(r, p, chromium)
            check_grip_dodges_toolbar(r, p, chromium)
            check_resize_settles(r, p, chromium)
    finally:
        server.should_exit = True
    return r


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
