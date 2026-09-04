"""Extension entry point.

Everything lives in the ``forge_canvas_ext`` package next to this file; this
module only puts it on the path and registers the WebUI callbacks. The name
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

from forge_canvas_ext import settings, transfer_log, ui_router  # noqa: E402

script_callbacks.on_ui_settings(settings.on_ui_settings)
script_callbacks.on_ui_tabs(ui_router.on_ui_tabs)
script_callbacks.on_app_started(transfer_log.on_app_started)
