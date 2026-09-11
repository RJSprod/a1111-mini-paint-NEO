"""Extension entry point.

Everything lives in the ``minipaint_neo`` package next to this file; this
module puts it on the path and registers the WebUI callbacks. The file name
and location are kept so existing installs keep working.
"""

import pathlib
import sys

try:
    _root = pathlib.Path(__file__).resolve().parents[1]
except NameError:  # pragma: no cover - only when exec'd without a __file__
    import inspect

    _root = pathlib.Path(inspect.getfile(lambda: None)).resolve().parents[1]

if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from modules import script_callbacks  # noqa: E402

from minipaint_neo import router, send_log, settings  # noqa: E402

script_callbacks.on_ui_settings(settings.on_ui_settings)
script_callbacks.on_ui_tabs(router.on_ui_tabs)
script_callbacks.on_app_started(send_log.on_app_started)

# The WanGP tab is a second, independent integration: its own tab, its own
# settings entry, its own routes, registered alongside the ones above rather
# than in place of them. Everything it does lives in minipaint_neo.wangp - the
# reverse proxy, the child process and the bridge must not leak into this
# file. If any of it cannot load, Mini Paint loads exactly as it did before
# and one line says why.
try:
    from minipaint_neo import wangp  # noqa: E402

    wangp.register(script_callbacks)
except Exception as _wangp_error:  # pragma: no cover - depends on the host
    from minipaint_neo import scrub  # noqa: E402

    scrub.console(f"the WanGP integration did not load ({_wangp_error}); Mini Paint is unaffected.")

# The touch Canvas puts a small "send to Canvas" button in each output panel.
# It is created by an ordinary component hook, so it is only registered when
# that frontend is the one being built; the legacy editor adds its own button
# from the browser, as it always has.
if not settings.use_old_ui():
    from minipaint_neo.canvas import host  # noqa: E402

    if hasattr(script_callbacks, "on_before_ui"):
        script_callbacks.on_before_ui(host.reset_capture)
    script_callbacks.on_after_component(host.on_after_component)
