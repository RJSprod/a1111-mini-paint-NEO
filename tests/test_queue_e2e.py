"""The queue, end to end: Clipboard composer, public API, bridge, WanGP's chain.

Section 28.5 of the Clipboard design intent, without a WanGP and without a
browser. A stand-in WanGP is a real Gradio app whose generator form has
Wan2GP's own variable names - ``prompt``, ``wizard_prompt``,
``wizard_prompt_activated_var``, ``client_id``, ``state``,
``add_to_queue_trigger`` and the rest - and whose trigger's ``.change`` does
what Wan2GP's native chain does in miniature: reads the form, copies the
client id into the task it queues under the page's ``state``, and records a
refusal under ``queue_errors`` instead when validation fails. The bridge
plugin is placed on that page through ``insert_after`` exactly as Wan2GP
places it, and everything is driven through Gradio's own predict endpoint
with a session hash, the way the browser drives it.

The test plays the browser: it holds the page's values, sends them as the
event's inputs, applies what the event wrote back, and fires the trigger's
change when the bridge changed the trigger - which is the only way the
stand-in's chain ever runs. So what the checks read in the queued task is
what WanGP would have read from its form: the page as it was, with the
request's overrides on top, and the page as it was again once the
admission was confirmed. The public API's server half (staging, preparing
handoffs) and the Clipboard composer's request and history sit at the two
ends of the same path.
"""

from harness import Results, setup_path

setup_path()

import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402

import forge_like  # noqa: E402,F401  (first: Forge's metaclass patches, before any Blocks is built)
from PIL import Image  # noqa: E402

from minipaint_neo import interop  # noqa: E402
from minipaint_neo.clipboard import config as clipboard_config  # noqa: E402
from minipaint_neo.clipboard import history  # noqa: E402
from minipaint_neo.clipboard import store as clipboard_store  # noqa: E402
from minipaint_neo.clipboard import ui as clipboard_ui  # noqa: E402
from minipaint_neo.wangp import config as wangp_config  # noqa: E402
from minipaint_neo.wangp import handoff, process_log, protocol  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE_DIR = ROOT / "wan2gp_bridge" / "wan2gp-minipaint-bridge"

VIDEO_MODEL = {
    "name": "A video model",
    "image_prompt_types_allowed": "TSEV",
    "image_ref_choices": {"choices": [("None", ""), ("People / Objects", "I"), ("Landscape then people", "KI")], "letters_filter": "KFI"},
}
TEXT_ONLY_MODEL = {"name": "Text only", "image_prompt_types_allowed": "T"}
PAGE_PROMPT = "the page's own prompt"

ID_A = "0123456789abcdef0123456789abcdef"
ID_B = "fedcba9876543210fedcba9876543210"
ID_C = "aaaaaaaabbbbbbbbccccccccdddddddd"
ID_D = "11112222333344445555666677778888"
ID_E = "9999aaaabbbbccccddddeeeeffff0000"
ID_F = "0000ffffeeeeddddccccbbbbaaaa9999"
ID_G = "1234123412341234123412341234abcd"
ID_H = "abcd1234abcd1234abcd1234abcd1234"


def _modules():
    added = str(BRIDGE_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(BRIDGE_DIR))
    try:
        import bridge_ui
        import compatibility
        import plugin
    finally:
        if added and str(BRIDGE_DIR) in sys.path:
            sys.path.remove(str(BRIDGE_DIR))
    return plugin, compatibility, bridge_ui


def _png(colour, size=(6, 4)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _predict(client, body):
    """Gradio's predict endpoint, wherever this Gradio keeps it (see test_wangp_protocol)."""
    for path in ("/gradio_api/run/predict", "/run/predict", "/api/predict"):
        response = client.post(path, json=body)
        if response.status_code != 404:
            return response
    return response


def _wangp_insertions(all_components, requests):
    """Wan2GP's own ``insert_after`` processing: the builder runs inside the
    target's container and its last child is moved behind the target."""
    placed = []
    for target_name, builder in requests:
        target = all_components.get(target_name)
        parent = getattr(target, "parent", None)
        if not target or not parent or not hasattr(parent, "children"):
            placed.append(None)
            continue
        target_index = parent.children.index(target)
        with parent:
            builder()
        newly_added = parent.children.pop(-1)
        parent.children.insert(target_index + 1, newly_added)
        placed.append(newly_added)
    return placed


class _FakeLoader:
    def __init__(self, sources):
        self.sources = dict(sources)

    def get_source(self, environment, template):
        return self.sources[template], "/templates/" + template, (lambda: True)


class _FakeTemplates:
    """Gradio's template object, as far as the plugin's head injection touches it."""

    def __init__(self):
        page = '<!doctype html><html><head><meta charset="utf-8">\n<script type="module" crossorigin src="./assets/index-abc.js"></script>\n</head><body></body></html>'
        self.env = type("Env", (), {})()
        self.env.loader = _FakeLoader({"frontend/index.html": page, "frontend/share.html": page})
        self.env.cache = type("Cache", (), {"clear": lambda cache: None})()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Wgp:
    """What the plugin sees of Wan2GP: the hooks, and the injected globals."""

    def __init__(self):
        self.definitions = {"video": VIDEO_MODEL, "text": TEXT_ONLY_MODEL}
        self.inserts = []
        self.scripts = []
        self.unique = 0

    def request_component(self, elem_id):
        pass

    def request_global(self, name):
        pass

    def add_custom_js(self, script):
        self.scripts.append(script)

    def insert_after(self, target, builder):
        self.inserts.append((target, builder))

    def get_model_def(self, model_type):
        return self.definitions.get(model_type)

    @staticmethod
    def get_state_model_type(state):
        return state.get("model_type")

    def get_unique_id(self):
        self.unique += 1
        return f"unique-{self.unique}"

    @staticmethod
    def get_gen_info(state):
        return state.setdefault("gen", {})


# --------------------------------------------------------- the stand-in --


def build_wangp(clock):
    """A WanGP-shaped Gradio app with the bridge placed on it. Returns everything a test needs."""
    import gradio as gr

    plugin_module, compatibility, _bridge_ui = _modules()
    wgp = _Wgp()
    plugin = plugin_module.MiniPaintBridgePlugin.__new__(plugin_module.MiniPaintBridgePlugin)
    plugin.bridge = plugin_module.MiniPaintBridge(
        host=compatibility.Host(wgp),
        environ={"MINIPAINT_WANGP_INSTANCE_ID": "i", "MINIPAINT_WANGP_HANDOFF_ROOT": str(handoff.handoff_root())},
        clock=clock,
    )
    plugin.bridge.compat.declare_globals()
    plugin.declared = False
    plugin.controls = None
    plugin.wired = False
    plugin.instances = 0
    plugin.injected = False
    plugin.page_templates = _FakeTemplates()
    plugin.add_custom_js = wgp.add_custom_js
    plugin.insert_after = wgp.insert_after

    def add_task(prompt_value, wizard_value, wizard_active, start, end, refs, client, state_value):
        """Wan2GP's trigger chain in miniature: validate_wizard_prompt ->
        save_inputs -> process_prompt_and_add_tasks. The client id goes into
        the task's params, which is what makes confirmation possible."""
        text = wizard_value if str(wizard_active).strip().lower() == "on" else prompt_value
        gen = state_value.setdefault("gen", {})
        if "REFUSE" in str(text):
            gen.setdefault("queue_errors", {})[client] = "the prompt was refused by validation"
            return state_value
        gen.setdefault("queue", []).append({
            "id": len(gen.get("queue", [])) + 1,
            "params": {"client_id": client, "prompt": text, "image_start": start, "image_end": end, "image_refs": refs},
        })
        return state_value

    def set_model(choice, state_value):
        state_value["model_type"] = choice
        return state_value

    def clear_queue(state_value):
        state_value.setdefault("gen", {})["queue"] = []
        return state_value

    with gr.Blocks(analytics_enabled=False) as demo:
        state = gr.State({"model_type": "video", "gen": {"queue": [], "queue_errors": {}}})
        current_gallery_tab = gr.State(0)
        model_choice = gr.Dropdown(["video", "text"], value="video", label="model")
        image_mode = gr.Number(value=0, visible=False)
        with gr.Row(visible=False) as image_start_row:
            image_start = gr.Gallery(label="start", type="pil")
        with gr.Row(visible=False) as image_end_row:
            image_end = gr.Gallery(label="end", type="pil")
        with gr.Row(visible=False) as image_refs_row:
            image_refs = gr.Gallery(label="refs", type="pil")
        image_prompt_type = gr.Text(value="T", visible=False)
        video_prompt_type = gr.Text(value="", visible=False)
        image_prompt_type_radio = gr.Text(value="T", visible=False)
        image_prompt_type_endcheckbox = gr.Checkbox(value=False, label="End image(s)")
        video_prompt_type_image_refs = gr.Text(value="", visible=False)
        prompt = gr.Textbox(value=PAGE_PROMPT, label="prompt")
        wizard_prompt = gr.Textbox(value="", label="wizard prompt")
        wizard_prompt_activated_var = gr.Text(value="off", visible=False)
        client_id = gr.Textbox(value="", visible=False)
        add_to_queue_trigger = gr.Text(value="", visible=False)
        clear_button = gr.Button("clear queue")
        add_to_queue_trigger.change(add_task, inputs=[prompt, wizard_prompt, wizard_prompt_activated_var, image_start, image_end, image_refs, client_id, state], outputs=[state])
        model_choice.change(set_model, inputs=[model_choice, state], outputs=[state])
        clear_button.click(clear_queue, inputs=[state], outputs=[state])

        handed = {
            "state": state, "current_gallery_tab": current_gallery_tab, "model_choice": model_choice, "image_mode": image_mode,
            "image_start_row": image_start_row, "image_start": image_start, "image_end_row": image_end_row, "image_end": image_end,
            "image_refs_row": image_refs_row, "image_refs": image_refs, "image_prompt_type": image_prompt_type,
            "video_prompt_type": video_prompt_type, "image_prompt_type_radio": image_prompt_type_radio,
            "image_prompt_type_endcheckbox": image_prompt_type_endcheckbox, "video_prompt_type_image_refs": video_prompt_type_image_refs,
            "prompt": prompt, "wizard_prompt": wizard_prompt, "wizard_prompt_activated_var": wizard_prompt_activated_var,
            "client_id": client_id, "add_to_queue_trigger": add_to_queue_trigger,
        }
        plugin.setup_ui()
        plugin.post_ui_setup(handed)
        _wangp_insertions(handed, wgp.inserts)

    initial = {
        "model_choice": "video", "image_mode": 0, "image_start": None, "image_end": None, "image_refs": None,
        "image_prompt_type": "T", "video_prompt_type": "", "image_prompt_type_radio": "T", "image_prompt_type_endcheckbox": False,
        "video_prompt_type_image_refs": "", "prompt": PAGE_PROMPT, "wizard_prompt": "", "wizard_prompt_activated_var": "off",
        "client_id": "", "add_to_queue_trigger": "",
    }
    return demo, plugin, wgp, handed, initial, {"trigger": add_to_queue_trigger, "model": model_choice, "clear": clear_button}


class Page:
    """One browser page on the stand-in: its values, and the events it fires.

    Sends the page's values as an event's inputs the way the frontend does,
    applies what the event wrote back, and fires the trigger's change when
    the bridge changed the trigger - the frontend's job, done here.
    """

    def __init__(self, client, config, session_hash, plugin, handed, initial, controls):
        self.client = client
        self.session = session_hash
        self.ids = {name: component._id for name, component in handed.items()}
        self.names = {component._id: name for name, component in handed.items()}
        self.values = {self.ids[name]: value for name, value in initial.items()}
        self.states = {self.ids["state"], self.ids["current_gallery_tab"]}
        self.request_id = plugin.controls.request._id
        self.ack_id = plugin.controls.ack._id
        self.values[self.request_id] = ""
        self.values[self.ack_id] = ""
        deps = config["dependencies"]
        self.bridge_dep = next(d for d in deps if [plugin.controls.trigger._id, "click"] in d["targets"])
        self.trigger_dep = next(d for d in deps if [controls["trigger"]._id, "change"] in d["targets"])
        self.model_dep = next(d for d in deps if [controls["model"]._id, "change"] in d["targets"])
        self.clear_dep = next(d for d in deps if [controls["clear"]._id, "click"] in d["targets"])
        self.fired = 0
        self.fire_trigger = True

    def value(self, name):
        return self.values[self.ids[name]]

    def set(self, name, value):
        self.values[self.ids[name]] = value

    def _call(self, dep, overrides=None):
        data = []
        for cid in dep["inputs"]:
            if cid in self.states:
                data.append(None)
            elif overrides and cid in overrides:
                data.append(overrides[cid])
            else:
                data.append(self.values.get(cid))
        response = _predict(self.client, {"data": data, "fn_index": dep["id"], "session_hash": self.session})
        if response.status_code != 200:
            raise RuntimeError(f"predict answered {response.status_code}: {response.text[:300]}")
        return response.json()["data"]

    def _apply(self, dep, data):
        changed = set()
        for cid, value in zip(dep["outputs"], data):
            if cid in self.states:
                continue
            if isinstance(value, dict) and value.get("__type__") == "update":
                if "value" not in value:
                    continue
                value = value["value"]
            if self.values.get(cid) != value:
                changed.add(cid)
            self.values[cid] = value
        return changed

    def bridge(self, request):
        """One bridge event: the request in, the acknowledgement out, the
        page updated, and the stand-in's chain run when the trigger moved."""
        data = self._call(self.bridge_dep, {self.request_id: json.dumps(request)})
        ack = json.loads(data[0])
        changed = self._apply(self.bridge_dep, data)
        if self.ids["add_to_queue_trigger"] in changed and self.fire_trigger:
            self.fired += 1
            self._apply(self.trigger_dep, self._call(self.trigger_dep))
        return ack

    def choose_model(self, name):
        self.set("model_choice", name)
        self._apply(self.model_dep, self._call(self.model_dep))

    def clear_queue(self):
        self._apply(self.clear_dep, self._call(self.clear_dep))

    def gallery_count(self, name):
        value = self.value(name)
        return len(value) if isinstance(value, list) else 0


class Stand:
    """The session's WanGP state, read back through the app the way get_gen_info reads it."""

    def __init__(self, app, session_hash, handed):
        self.app = app
        self.session = session_hash
        self.state_id = handed["state"]._id

    def gen(self):
        return self.app.state_holder[self.session][self.state_id].get("gen", {})

    def tasks(self):
        return list(self.gen().get("queue", []))

    def tasks_for(self, request_id):
        return [task for task in self.tasks() if task["params"]["client_id"] == request_id]


def _pixels(entry):
    """The one colour of a small stand-in picture, from whatever a gallery holds."""
    image = entry[0] if isinstance(entry, (tuple, list)) else entry
    if isinstance(image, str):
        image = Image.open(image)
    return image.convert("RGBA").getpixel((0, 0))


def _about(entry, colour, tolerance=12) -> bool:
    """The picture's colour, within what Gradio's lossy WebP cache moves it by."""
    try:
        found = _pixels(entry)
    except Exception:
        return False
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(found, colour))


def _images(task, field):
    value = task["params"].get(field)
    return list(value) if isinstance(value, list) else []


# ------------------------------------------------------------- the checks --


def run_checks(r: Results, base: pathlib.Path) -> None:
    from gradio.routes import App
    from starlette.testclient import TestClient

    clock = _Clock()
    demo, plugin, wgp, handed, initial, controls = build_wangp(clock)
    r.check("the bridge placed its controls on the stand-in and wired its event", plugin.controls is not None and plugin.wired is True, str(plugin.wired))
    r.check("the browser half was handed to WanGP once", len(wgp.scripts) == 1)
    app = App.create_app(demo)
    client = TestClient(app)
    config = client.get("/config").json()
    page = Page(client, config, "page-one", plugin, handed, initial, controls)
    stand = Stand(app, "page-one", handed)
    session_of = lambda: plugin_session  # noqa: E731

    # -- the handshake, through Gradio, says the queue is on
    hello = page.bridge({"op": "hello", "request_id": "r1", "channel_id": "c" * 32})
    plugin_session = hello.get("bridge_session")
    r.check("a hello through Gradio is ready and offers the queue",
            hello.get("ready") is True and hello.get("capabilities", {}).get("queue") is True and protocol.valid_handoff_id(plugin_session or ""), json.dumps(hello)[:200])
    r.check("and speaks protocol 3", hello.get("protocol") == 3 and hello.get("bridge_version") == "1.2.0")

    def queue(request_id, **fields):
        body = {"request_id": request_id, "bridge_session": session_of()}
        body.update(fields)
        return page.bridge({"op": "queue", "request_id": "q-" + request_id[:8], "channel_id": "c" * 32, "queue": body})

    def confirm(request_id):
        return page.bridge({"op": "confirm", "request_id": "k-" + request_id[:8], "channel_id": "c" * 32, "queue": {"request_id": request_id, "bridge_session": session_of()}})

    # -- 1. nothing supplied: the live page, exactly as it is
    ack = queue(ID_A)
    r.check("an empty request is admitted", ack.get("ok") is True and ack.get("admission") == "requested", json.dumps(ack)[:200])
    r.check("every field is inherited, none applied", ack.get("inherited") == list(protocol.QUEUE_FIELDS) and not any(ack["applied"].values()), json.dumps(ack.get("applied")))
    r.check("the bridge wrote the client id and the trigger, and WanGP's chain ran once", page.value("client_id") == ID_A and page.fired == 1, page.value("client_id"))
    tasks = stand.tasks_for(ID_A)
    r.check("WanGP queued one task carrying the request as its client id", len(tasks) == 1 and stand.tasks() == tasks)
    r.check("with the page's own prompt and no images", tasks and tasks[0]["params"]["prompt"] == PAGE_PROMPT and not _images(tasks[0], "image_start") and not _images(tasks[0], "image_refs"))
    r.check("the prompt box was never touched", page.value("prompt") == PAGE_PROMPT)
    status = confirm(ID_A)
    r.check("confirmation finds the task and says queued", status.get("ok") is True and status.get("status") == "queued" and status.get("tasks_added") == 1, json.dumps(status)[:200])
    r.check("and puts the client id back to what it was", page.value("client_id") == "" and "client_id" in (status.get("restored") or []), str(status.get("restored")))
    r.check("the answer names the model of the page", status.get("model", {}).get("label") == "A video model")

    # -- 2. overrides through the public API's server half: a staged image, a Clipboard asset, a prompt
    library = clipboard_store.store()
    library.set_root(str(base / "library"), create=True)
    ref_asset = library.import_bytes(_png((30, 10, 200, 255)), "ref.png", "upload")
    staged = interop.stage_bytes(_png((10, 200, 30, 255)), "image/png")
    public = interop.normalize_public_request({
        "request_id": ID_B, "prompt": "  an override prompt \x07 ",
        "images": {"start": staged["image"], "references": [{"kind": "clipboard_asset", "id": ref_asset.asset_id}], "end": None},
    })
    r.check("the public request normalises: prompt cleaned, end omitted", public["prompt"] == "an override prompt" and "end" not in public["images"], json.dumps(public)[:200])
    wire = interop.prepare(public)
    r.check("preparation turns the handles into ordinary handoffs under the handoff root",
            protocol.valid_handoff_id(wire.get("start_handoff_id", "")) and len(wire.get("reference_handoff_ids", [])) == 1 and set(wire["handoff_ids"]) == {wire["start_handoff_id"], *wire["reference_handoff_ids"]}
            and all((handoff.handoff_root() / f"{item}.png").is_file() for item in wire["handoff_ids"]), json.dumps(wire))
    r.check("and the wire request carries ids and the prompt, never a path or a kind", "/" not in json.dumps(wire) and "clipboard_asset" not in json.dumps(wire))
    before = page.fired
    ack = queue(ID_B, prompt=wire["prompt"], start_handoff_id=wire["start_handoff_id"], reference_handoff_ids=wire["reference_handoff_ids"])
    r.check("the overlay is admitted", ack.get("ok") is True and ack.get("admission") == "requested", json.dumps(ack)[:200])
    r.check("applied: prompt, start, one reference; inherited: end",
            ack["applied"] == {"prompt": True, "start": True, "end": False, "references": 1} and ack["inherited"] == ["end"] and ack["ignored"] == [], json.dumps(ack["applied"]))
    r.check("the acknowledgement never carries the prompt text", "override prompt" not in json.dumps(ack))
    r.check("the page now shows the override in the prompt box, one start frame, one reference",
            page.value("prompt") == "an override prompt" and page.gallery_count("image_start") == 1 and page.gallery_count("image_refs") == 1 and page.gallery_count("image_end") == 0)
    r.check("with the selectors switched on the way a click would switch them",
            "S" in page.value("image_prompt_type") and page.value("image_prompt_type_radio") == "S" and "I" in page.value("video_prompt_type") and page.value("video_prompt_type_image_refs") == "I", f"{page.value('image_prompt_type')}/{page.value('video_prompt_type')}")
    r.check("WanGP's chain ran once more", page.fired == before + 1)
    tasks = stand.tasks_for(ID_B)
    r.check("the queued task holds the override prompt", len(tasks) == 1 and tasks[0]["params"]["prompt"] == "an override prompt")
    r.check("the staged picture as its start frame and the Clipboard asset as its reference, by pixels",
            tasks and len(_images(tasks[0], "image_start")) == 1 and _about(_images(tasks[0], "image_start")[0], (10, 200, 30, 255))
            and len(_images(tasks[0], "image_refs")) == 1 and _about(_images(tasks[0], "image_refs")[0], (30, 10, 200, 255)))
    r.check("and the end frame the page had: none", tasks and not _images(tasks[0], "image_end"))
    status = confirm(ID_B)
    r.check("confirmed queued", status.get("status") == "queued" and status.get("tasks_added") == 1, json.dumps(status)[:200])
    r.check("the prompt, the galleries and the selectors are put back",
            page.value("prompt") == PAGE_PROMPT and page.gallery_count("image_start") == 0 and page.gallery_count("image_refs") == 0
            and page.value("image_prompt_type") == "T" and page.value("image_prompt_type_radio") == "T" and page.value("video_prompt_type") == "" and page.value("client_id") == "",
            f"{page.value('prompt')!r} {page.gallery_count('image_start')} {page.value('image_prompt_type')!r}")
    r.check("and the answer lists what was restored", {"prompt", "start_image", "reference_gallery", "client_id"} <= set(status.get("restored") or []) and not status.get("restore_skipped"), str(status.get("restored")))
    released = interop.release(wire["handoff_ids"])
    r.check("the handoffs are released once the bridge has them", released == 2 and not any((handoff.handoff_root() / f"{item}.png").exists() for item in wire["handoff_ids"]))
    r.check("the queued task keeps its pictures after the release", _about(_images(stand.tasks_for(ID_B)[0], "image_start")[0], (10, 200, 30, 255)))

    # -- 3. the same request again is a duplicate, and the same id for another payload a conflict
    count = len(stand.tasks())
    ack = queue(ID_B, prompt=wire["prompt"], start_handoff_id=wire["start_handoff_id"], reference_handoff_ids=wire["reference_handoff_ids"])
    r.check("a retry of an admitted request is answered from the record and queues nothing again",
            ack.get("admission") == "duplicate" and ack.get("status") == "queued" and len(stand.tasks()) == count and page.value("client_id") == "", json.dumps(ack)[:200])
    ack = queue(ID_B, prompt="something else")
    r.check("the same id with another payload is REQUEST_ID_CONFLICT", ack.get("ok") is False and ack.get("code") == "REQUEST_ID_CONFLICT" and len(stand.tasks()) == count, json.dumps(ack)[:200])

    # -- 4. a field the live model cannot take is reported and left out; the rest still queues
    page.choose_model("text")
    hello = page.bridge({"op": "receivers", "request_id": "r2", "channel_id": "c" * 32})
    r.check("on a text-only model no image input is offered", hello.get("ready") is True and not any(item.get("enabled") for item in hello.get("receivers", [])), json.dumps(hello.get("receivers"))[:200])
    staged = interop.stage_bytes(_png((90, 90, 90, 255)), "image/png")
    wire = interop.prepare(interop.normalize_public_request({"request_id": ID_C, "prompt": "text only", "images": {"start": staged["image"]}}))
    ack = queue(ID_C, prompt="text only", start_handoff_id=wire["start_handoff_id"])
    r.check("the request is still admitted", ack.get("ok") is True and ack.get("admission") == "requested", json.dumps(ack)[:200])
    r.check("with the start frame ignored as RECEIVER_DISABLED and the prompt applied",
            ack["ignored"] == [{"field": "start", "code": "RECEIVER_DISABLED"}] and ack["applied"]["prompt"] is True and ack["applied"]["start"] is False, json.dumps(ack["ignored"]))
    tasks = stand.tasks_for(ID_C)
    r.check("WanGP queued it with the prompt and without the picture", len(tasks) == 1 and tasks[0]["params"]["prompt"] == "text only" and not _images(tasks[0], "image_start"))
    r.check("and the start gallery was never written", page.gallery_count("image_start") == 0 and "S" not in page.value("image_prompt_type"))
    status = confirm(ID_C)
    r.check("confirmed, with the ignored field still in the answer", status.get("status") == "queued" and status.get("ignored") == [{"field": "start", "code": "RECEIVER_DISABLED"}])
    interop.release(wire["handoff_ids"])
    page.choose_model("video")

    # -- 5. one request at a time per page: the form has an owner until it is confirmed
    ack_d = queue(ID_D, prompt="first of two")
    ack_e = queue(ID_E, prompt="second of two")
    r.check("a second request while the first is pending is QUEUE_BUSY, and touches nothing",
            ack_d.get("admission") == "requested" and ack_e.get("ok") is False and ack_e.get("code") == "QUEUE_BUSY" and page.value("client_id") == ID_D and page.value("prompt") == "first of two", json.dumps(ack_e)[:200])
    r.check("the busy answer still names the request it refused", ack_e.get("request_id") == "q-" + ID_E[:8] and ack_e.get("queue_request_id") == ID_E)
    status = confirm(ID_D)
    r.check("once the first is confirmed", status.get("status") == "queued" and page.value("prompt") == PAGE_PROMPT)
    ack_e = queue(ID_E, prompt="second of two")
    r.check("the second goes through", ack_e.get("admission") == "requested" and len(stand.tasks_for(ID_E)) == 1, json.dumps(ack_e)[:200])
    status = confirm(ID_E)
    r.check("and is confirmed on its own", status.get("status") == "queued" and status.get("queue_request_id") == ID_E)

    # -- 6. queued sticks: the queue emptying later does not change the answer
    page.clear_queue()
    r.check("(the stand-in's queue is empty now)", stand.tasks() == [])
    status = confirm(ID_E)
    r.check("a request once seen queued stays queued", status.get("status") == "queued" and status.get("tasks_added") == 1)

    # -- 7. a refusal is only a refusal on WanGP's own, correlated evidence
    ack = queue(ID_F, prompt="please REFUSE this one")
    status = confirm(ID_F)
    r.check("a prompt WanGP's validation refuses is refused, with the code, and no task",
            ack.get("admission") == "requested" and status.get("status") == "refused" and status.get("code") == "WANGP_VALIDATION_REFUSED" and not stand.tasks_for(ID_F), json.dumps(status)[:200])
    r.check("and the prompt box is put back all the same", page.value("prompt") == PAGE_PROMPT and page.value("client_id") == "")

    # -- 8. no task and no error is pending, then unconfirmed when the time is up
    page.fire_trigger = False
    ack = queue(ID_G, prompt="nobody runs the chain")
    status = confirm(ID_G)
    r.check("without a task and without an error the request is still pending", ack.get("admission") == "requested" and status.get("status") == "pending", json.dumps(status)[:200])
    r.check("and the overlay is still on the page while it is", page.value("prompt") == "nobody runs the chain")
    clock.now += protocol.PENDING_ADMISSION_SECONDS + 1
    status = confirm(ID_G)
    r.check("when the time is up it is expired with ADMISSION_UNCONFIRMED - never refused",
            status.get("status") == "expired" and status.get("code") == "ADMISSION_UNCONFIRMED", json.dumps(status)[:200])
    r.check("and the overlay is put back then", page.value("prompt") == PAGE_PROMPT and page.value("client_id") == "")
    page.fire_trigger = True

    # -- 9. compare before restore: what the user changed meanwhile is theirs
    staged = interop.stage_bytes(_png((1, 2, 3, 255)), "image/png")
    wire = interop.prepare(interop.normalize_public_request({"request_id": ID_H, "prompt": "to be edited", "images": {"start": staged["image"]}}))
    page.fire_trigger = False
    ack = queue(ID_H, prompt="to be edited", start_handoff_id=wire["start_handoff_id"])
    page.set("prompt", "the user typed over it")
    page.fire_trigger = True
    page._apply(page.trigger_dep, page._call(page.trigger_dep))  # the chain runs late, on the edited form
    status = confirm(ID_H)
    r.check("a value the user changed after the write is not restored, the rest is",
            status.get("status") == "queued" and page.value("prompt") == "the user typed over it" and page.gallery_count("image_start") == 0
            and "prompt" in (status.get("restore_skipped") or []) and "start_image" in (status.get("restored") or []), json.dumps(status)[:200])
    interop.release(wire["handoff_ids"])
    page.set("prompt", PAGE_PROMPT)

    # -- 10. the prompt goes where generation reads it: the wizard box while the wizard is on
    page.set("wizard_prompt_activated_var", "on")
    page.set("wizard_prompt", "the wizard's own text")
    ack = queue("77777777aaaaaaaabbbbbbbbcccccccc", prompt="through the wizard")
    r.check("with the wizard on the override lands in the wizard box and the plain box is left alone",
            page.value("wizard_prompt") == "through the wizard" and page.value("prompt") == PAGE_PROMPT, page.value("wizard_prompt"))
    tasks = stand.tasks_for("77777777aaaaaaaabbbbbbbbcccccccc")
    r.check("and that is what WanGP queued", len(tasks) == 1 and tasks[0]["params"]["prompt"] == "through the wizard")
    status = confirm("77777777aaaaaaaabbbbbbbbcccccccc")
    r.check("and what is put back afterwards", status.get("status") == "queued" and page.value("wizard_prompt") == "the wizard's own text")
    page.set("wizard_prompt_activated_var", "off")

    # -- 11. the Clipboard composer at one end, its history at the other
    tab = clipboard_ui.ClipboardTab()
    first = library.import_bytes(_png((200, 30, 30, 255)), "first.png", "upload")
    tab.assign("first", first.asset_id)
    session = {"pending": {}}
    instruction, line, session = tab.prepare_queue("from the Clipboard tab", session)
    request = json.loads(instruction)["request"]
    r.check("the composer's request names its asset by id and carries the prompt", request["images"]["start"] == {"kind": "clipboard_asset", "id": first.asset_id} and request["prompt"] == "from the Clipboard tab")
    wire = interop.prepare(interop.normalize_public_request(request))
    ack = queue(request["request_id"], prompt=wire["prompt"], start_handoff_id=wire["start_handoff_id"])
    status = confirm(request["request_id"])
    tasks = stand.tasks_for(request["request_id"])
    r.check("it queues on WanGP with the Clipboard picture as the start frame",
            status.get("status") == "queued" and len(tasks) == 1 and _about(_images(tasks[0], "image_start")[0], (200, 30, 30, 255)) and tasks[0]["params"]["prompt"] == "from the Clipboard tab")
    answer = {"request_id": request["request_id"], "ok": True, "status": "queued", "tasks_added": status.get("tasks_added"),
              "applied": status.get("applied"), "inherited": status.get("inherited"), "ignored": status.get("ignored"), "model": status.get("model")}
    text, listing, session = tab.queue_result(json.dumps(answer), session)
    record = history.load_history()[0]
    r.check("and the tab's history records exactly that recipe",
            text.startswith("Added to WanGP queue.") and record["request_id"] == request["request_id"] and record["first_mode"] == "override" and record["first_asset_id"] == first.asset_id
            and record["prompt_mode"] == "override" and record["last_mode"] == "inherit" and record["model_label"] == "A video model", json.dumps(record))
    interop.release(wire["handoff_ids"])

    # -- 12. another page is another owner
    other = Page(client, config, "page-two", plugin, handed, initial, controls)
    other_hello = other.bridge({"op": "hello", "request_id": "r3", "channel_id": "d" * 32})
    r.check("a second page gets its own bridge session", protocol.valid_handoff_id(other_hello.get("bridge_session", "")) and other_hello["bridge_session"] != plugin_session)
    page.fire_trigger = False
    ack = queue("5555555566666666777777778888888", prompt="pending on page one")  # 31 chars: invalid on purpose
    r.check("a request id that is not 32 hex characters is REQUEST_INVALID before anything is written", ack.get("ok") is False and ack.get("code") == "REQUEST_INVALID" and page.value("client_id") == "")
    ack = queue("55555555666666667777777788888888", prompt="pending on page one")
    ack_other = other.bridge({"op": "queue", "request_id": "q-other", "channel_id": "d" * 32,
                              "queue": {"request_id": "66666666777777778888888899999999", "bridge_session": other_hello["bridge_session"], "prompt": "on page two"}})
    r.check("page two is not blocked by page one's pending request", ack.get("admission") == "requested" and ack_other.get("admission") == "requested", json.dumps(ack_other)[:200])
    r.check("and page two's WanGP session has its own task", len(Stand(app, "page-two", handed).tasks()) == 1 and other.value("prompt") == "on page two")
    ack_wrong = queue("99999999888888887777777766666666", bridge_session=other_hello["bridge_session"], prompt="for the wrong page")
    r.check("a request prepared for another page's session is BRIDGE_SESSION_MISMATCH", ack_wrong.get("ok") is False and ack_wrong.get("code") == "BRIDGE_SESSION_MISMATCH")
    page.fire_trigger = True

    # -- 13. nothing written down names the user
    log = process_log.path()
    written = pathlib.Path(log).read_text(encoding="utf-8") if os.path.isfile(log) else ""
    r.check("the Forge-side log holds no prompt text, no filename and no path",
            "override prompt" not in written and "first.png" not in written and str(base) not in written and "from the Clipboard tab" not in written, written[-300:])


def run() -> Results:
    r = Results("queue e2e")
    with tempfile.TemporaryDirectory(prefix="minipaint-queue-e2e-") as scratch:
        base = pathlib.Path(scratch)
        saved_tmp = os.environ.get("GRADIO_TEMP_DIR")
        os.environ["GRADIO_TEMP_DIR"] = str(base / "gradio-cache")
        wangp_config.use_config_dir(base / "data")
        clipboard_config.use_config_dir(base / "data")
        process_log.use_log_dir(base / "logs")
        clipboard_store.reset_for_tests()
        try:
            run_checks(r, base)
        finally:
            wangp_config.use_config_dir(None)
            clipboard_config.use_config_dir(None)
            process_log.use_log_dir(None)
            clipboard_store.reset_for_tests()
            if saved_tmp is None:
                os.environ.pop("GRADIO_TEMP_DIR", None)
            else:
                os.environ["GRADIO_TEMP_DIR"] = saved_tmp
    return r


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
