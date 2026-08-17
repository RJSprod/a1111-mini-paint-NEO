/**
 * Compatibility layer between miniPaint (running inside an iframe) and the
 * host WebUI (AUTOMATIC1111 / Forge / Forge Neo) running in the parent frame.
 *
 * Everything host-specific lives here. The rest of miniPaint should never
 * touch parent DOM internals directly.
 *
 * Design rule: adapters are chosen by inspecting the *target component*, never
 * by the host's Gradio version. Forge Neo is heterogeneous - img2img, inpaint
 * and ControlNet inputs are ForgeCanvas, while Extras is an ordinary gr.Image.
 */

export const LOG_PREFIX = 'MiniPaint:';

/** How long a transfer may take before it is reported as failed. */
export const TRANSFER_TIMEOUT_MS = 10000;

/**
 * How long to wait for a component to *display* what we committed. This is a
 * consistency check on top of the backing value, not the success criterion,
 * so it is deliberately shorter than TRANSFER_TIMEOUT_MS.
 */
const UI_ACK_TIMEOUT_MS = 3000;

/**
 * How long a ForgeCanvas needs, given the size of the image.
 *
 * It decodes the data URL into an <img>, draws it, then re-encodes the whole
 * canvas back to PNG, and all of that scales with the image. Measured in
 * Chromium against Forge Neo's own canvas.js: 0.8MB took 0.34s, 3MB 2.1s,
 * 11MB 8.8s, 27MB 21s. A fixed timeout is therefore not a timeout at all - it
 * is a size limit, and a quiet one, because the image does land, just later
 * than the check looked.
 */
export function transfer_budget(data_url) {
	const megabytes = (data_url ? data_url.length : 0) / 1048576;
	const display_ms = Math.min(120000, Math.round(5000 + megabytes * 2500));
	return {
		megabytes: Math.round(megabytes * 100) / 100,
		display_ms,
		// Settling and the framework catching up are quick once the decode is
		// done, but they still grow with the value being copied around.
		settle_ms: Math.min(30000, Math.round(2000 + megabytes * 500)),
	};
}

/** Stable wrapper ids for the "send to" destinations. */
export const DESTINATIONS = {
	img2img_img2img: '#img2img_image',
	img2img_inpaint: '#img2maskimg',
	extras: '#extras_image',
};

export function log_error(message, error) {
	if (error !== undefined) {
		console.error(`${LOG_PREFIX} ${message}`, error);
	} else {
		console.error(`${LOG_PREFIX} ${message}`);
	}
}

export function log_warning(message, error) {
	if (error !== undefined) {
		console.warn(`${LOG_PREFIX} ${message}`, error);
	} else {
		console.warn(`${LOG_PREFIX} ${message}`);
	}
}

export function log_info(message) {
	console.log(`${LOG_PREFIX} ${message}`);
}

/** The parent window, or null when miniPaint runs standalone. */
export function host_window() {
	try {
		return window.parent && window.parent !== window ? window.parent : null;
	} catch (e) {
		return null;
	}
}

/**
 * Root node to query the host UI from. Prefers the WebUI's own gradioApp()
 * helper so we resolve inside the gradio-app shadow root when there is one.
 */
export function root() {
	const parent_window = host_window();
	if (!parent_window) {
		return document;
	}
	try {
		if (typeof parent_window.gradioApp === 'function') {
			return parent_window.gradioApp() || parent_window.document;
		}
	} catch (e) {
		/* fall through */
	}
	return parent_window.document;
}

export function query(selector) {
	try {
		return root().querySelector(selector) || null;
	} catch (e) {
		return null;
	}
}

export function must_query(selector) {
	const element = query(selector);
	if (!element) {
		throw new Error(`${LOG_PREFIX} missing element: ${selector}`);
	}
	return element;
}

/**
 * Resolve a selector once it exists in the host DOM.
 * Gradio 4 hydrates asynchronously, so nothing may be present on first look.
 */
export function wait_for_selector(selector, timeout_ms = 10000) {
	const existing = query(selector);
	if (existing) {
		return Promise.resolve(existing);
	}

	return new Promise((resolve, reject) => {
		let observer = null;
		const target = root();

		const timer = setTimeout(() => {
			if (observer) {
				observer.disconnect();
			}
			reject(new Error(`${LOG_PREFIX} timed out after ${timeout_ms}ms waiting for ${selector}`));
		}, timeout_ms);

		observer = new MutationObserver(() => {
			const element = query(selector);
			if (element) {
				clearTimeout(timer);
				observer.disconnect();
				resolve(element);
			}
		});

		observer.observe(target, { childList: true, subtree: true });
	});
}

/**
 * Resolve a destination, opening its tab/accordion only if it is not mounted.
 *
 * Returning `opened` lets the caller know whether the user was already moved
 * away from miniPaint: a transfer that fails afterwards cannot pretend the
 * user is still sitting in the editor.
 */
export async function resolve_target(selector, open_target) {
	const existing = query(selector);
	if (existing) {
		return { wrapper: existing, opened: false };
	}

	if (typeof open_target === 'function') {
		open_target();
	}

	const wrapper = await wait_for_selector(selector, TRANSFER_TIMEOUT_MS);
	return { wrapper, opened: true };
}

/* ------------------------------------------------------------------ */
/* Awaiting host state                                                 */
/* ------------------------------------------------------------------ */

function pause(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Poll a synchronous predicate until it is truthy.
 * Everything we wait on is host state we do not own (Gradio's uploader,
 * ForgeCanvas' 100ms textarea poll), so this is bounded by a deadline rather
 * than trusting a single event.
 *
 * `stable_ms` additionally requires the predicate to keep holding for that
 * long. Values that a late server reply can still overwrite are only worth
 * trusting once they have stopped moving.
 */
export function wait_until(predicate, options = {}) {
	const timeout_ms = options.timeout_ms || TRANSFER_TIMEOUT_MS;
	const interval_ms = options.interval_ms || 50;
	const stable_ms = options.stable_ms || 0;
	const description = options.description || 'condition';

	return new Promise((resolve, reject) => {
		const deadline = Date.now() + timeout_ms;
		let holding_since = null;

		const tick = () => {
			let value;
			try {
				value = predicate();
			} catch (e) {
				reject(e);
				return;
			}

			if (value) {
				if (!stable_ms) {
					resolve(value);
					return;
				}
				if (holding_since === null) {
					holding_since = Date.now();
				} else if (Date.now() - holding_since >= stable_ms) {
					resolve(value);
					return;
				}
			} else {
				holding_since = null;
			}

			if (Date.now() >= deadline) {
				reject(new Error(`${LOG_PREFIX} timed out after ${timeout_ms}ms waiting for ${description}`));
				return;
			}
			setTimeout(tick, interval_ms);
		};

		tick();
	});
}

/* ------------------------------------------------------------------ */
/* Writing into host components                                        */
/* ------------------------------------------------------------------ */

/** The realms a host element may sensibly be scripted from, best first. */
function realms_for(element) {
	const candidates = [];
	try {
		const view = element && element.ownerDocument && element.ownerDocument.defaultView;
		if (view) {
			candidates.push(view);
		}
	} catch (e) {
		/* cross-realm access can throw; keep the fallbacks */
	}
	const parent_window = host_window();
	if (parent_window && candidates.indexOf(parent_window) === -1) {
		candidates.push(parent_window);
	}
	if (candidates.indexOf(window) === -1) {
		candidates.push(window);
	}
	return candidates;
}

/**
 * Set .value through the prototype setter of the element's own realm.
 * Frameworks that shadow the property (and any host that later swaps Gradio's
 * front end) only observe writes that go through the native setter.
 */
export function set_native_value(element, value) {
	const property = element.tagName === 'TEXTAREA' ? 'HTMLTextAreaElement' : 'HTMLInputElement';

	for (const realm of realms_for(element)) {
		try {
			const descriptor = Object.getOwnPropertyDescriptor(realm[property].prototype, 'value');
			if (descriptor && descriptor.set) {
				descriptor.set.call(element, value);
				return true;
			}
		} catch (e) {
			/* try the next realm */
		}
	}

	element.value = value;
	return false;
}

/** Dispatch an event built by the element's own realm. */
export function dispatch_host_event(element, type, init = { bubbles: true }) {
	for (const realm of realms_for(element)) {
		try {
			element.dispatchEvent(new realm.Event(type, init));
			return true;
		} catch (e) {
			/* try the next realm */
		}
	}
	return false;
}

export function is_png_data_url(value) {
	return typeof value === 'string' && value.indexOf('data:image/png;base64,') === 0;
}

/* ------------------------------------------------------------------ */
/* Reading what the WebUI will actually submit                         */
/* ------------------------------------------------------------------ */

/**
 * The live value Gradio holds for a component - the one it sends when the
 * user presses Generate.
 *
 * gradio_config.components[].props is the same object the running front end
 * mutates, so this is the submitted value itself rather than the DOM node it
 * happens to be mirrored in. Not every host/Gradio build keeps it in sync, so
 * callers must handle `readable: false`.
 */
export function gradio_component_value(elem_id, class_name) {
	const realm = host_window() || window;
	try {
		const components = realm.gradio_config && realm.gradio_config.components;
		if (!Array.isArray(components)) {
			return { readable: false, reason: 'gradio_config.components not available' };
		}
		for (const component of components) {
			const props = component.props || {};
			if (props.elem_id !== elem_id) {
				continue;
			}
			if (class_name && (props.elem_classes || []).indexOf(class_name) === -1) {
				continue;
			}
			return { readable: true, value: props.value, component_id: component.id };
		}
		return { readable: false, reason: `no component with elem_id ${elem_id}` };
	} catch (e) {
		return { readable: false, reason: 'gradio_config could not be read' };
	}
}

/* ------------------------------------------------------------------ */
/* Comparing images                                                    */
/* ------------------------------------------------------------------ */

/** Side of the thumbnail images are reduced to before being compared. */
const SIGNATURE_SIZE = 32;

/**
 * Mean per-channel difference two images may show and still count as the same
 * picture. A faithful transfer stays near zero even when the host re-encodes
 * the PNG; a stale, blank or truncated image is far above it.
 */
const SIGNATURE_TOLERANCE = 4;

/**
 * Decode an image and describe it: exact size, exact byte length, a hash of
 * the encoded string, and a downscaled pixel signature to compare against.
 */
export function image_signature(data_url) {
	return new Promise((resolve, reject) => {
		if (typeof data_url !== 'string' || !data_url) {
			reject(new Error(`${LOG_PREFIX} there is no image to describe`));
			return;
		}

		const image = new Image();
		image.onload = () => {
			const canvas = document.createElement('canvas');
			canvas.width = SIGNATURE_SIZE;
			canvas.height = SIGNATURE_SIZE;
			const context = canvas.getContext('2d', { willReadFrequently: true });
			context.clearRect(0, 0, SIGNATURE_SIZE, SIGNATURE_SIZE);
			context.drawImage(image, 0, 0, SIGNATURE_SIZE, SIGNATURE_SIZE);

			const pixels = context.getImageData(0, 0, SIGNATURE_SIZE, SIGNATURE_SIZE).data;

			let opaque = 0;
			let red = 0;
			let green = 0;
			let blue = 0;
			for (let i = 0; i < pixels.length; i += 4) {
				if (pixels[i + 3] > 0) {
					opaque++;
					red += pixels[i];
					green += pixels[i + 1];
					blue += pixels[i + 2];
				}
			}

			resolve({
				width: image.naturalWidth,
				height: image.naturalHeight,
				length: data_url.length,
				hash: string_hash(data_url),
				pixels,
				// What the picture is actually like, so a log can say whether
				// the other side is holding a blank, a different image, or the
				// same one re-encoded.
				coverage: Math.round((opaque / (SIGNATURE_SIZE * SIGNATURE_SIZE)) * 100),
				mean: opaque
					? `rgb(${Math.round(red / opaque)}, ${Math.round(green / opaque)}, ${Math.round(blue / opaque)})`
					: 'nothing visible',
			});
		};
		image.onerror = () => reject(new Error(`${LOG_PREFIX} the image could not be decoded`));
		image.src = data_url;
	});
}

/** FNV-1a, so two values can be named in a log without printing megabytes. */
function string_hash(text) {
	let hash = 0x811c9dc5;
	for (let i = 0; i < text.length; i++) {
		hash ^= text.charCodeAt(i);
		hash = Math.imul(hash, 0x01000193);
	}
	return (hash >>> 0).toString(16);
}

/** A row-by-row sketch of a signature, for logs that have to be read as text. */
export function signature_thumbnail(signature) {
	const shades = ' .:-=+*#%@';
	const rows = [];

	for (let y = 0; y < SIGNATURE_SIZE; y += 2) {
		let row = '';
		for (let x = 0; x < SIGNATURE_SIZE; x++) {
			const index = (y * SIGNATURE_SIZE + x) * 4;
			const alpha = signature.pixels[index + 3] / 255;
			const luminance =
				(signature.pixels[index] * 0.299 +
					signature.pixels[index + 1] * 0.587 +
					signature.pixels[index + 2] * 0.114) /
				255;
			row += alpha === 0 ? ' ' : shades[Math.min(shades.length - 1, Math.round(luminance * alpha * 9))];
		}
		rows.push(`|${row}|`);
	}
	return rows;
}

/**
 * Is `committed` the same picture as `sent`?
 *
 * Compared with the colours weighted by their alpha, because a canvas round
 * trip discards whatever was underneath a fully transparent pixel: without
 * that, an image with transparent areas comes back "different" despite being
 * the same picture.
 */
export function compare_signatures(sent, committed) {
	if (sent.width !== committed.width || sent.height !== committed.height) {
		return {
			same: false,
			reason: `it is ${committed.width}x${committed.height}, not ${sent.width}x${sent.height}`,
		};
	}

	let total = 0;
	for (let i = 0; i < sent.pixels.length; i += 4) {
		const sent_alpha = sent.pixels[i + 3];
		const held_alpha = committed.pixels[i + 3];
		for (let channel = 0; channel < 3; channel++) {
			total += Math.abs(
				(sent.pixels[i + channel] * sent_alpha) / 255 - (committed.pixels[i + channel] * held_alpha) / 255
			);
		}
		total += Math.abs(sent_alpha - held_alpha);
	}
	const difference = total / sent.pixels.length;

	return {
		same: difference <= SIGNATURE_TOLERANCE,
		difference: Math.round(difference * 100) / 100,
		byte_identical: sent.length === committed.length && sent.hash === committed.hash,
		reason:
			difference <= SIGNATURE_TOLERANCE
				? null
				: `its pixels differ from the sent image by ${Math.round(difference)}/255 on average`,
	};
}

/**
 * Classify an image destination by what it actually contains.
 *   forge-canvas  - ForgeCanvas (img2img, inpaint, ControlNet)
 *   gradio-image  - ordinary gr.Image (Extras on Forge Neo, everything on A1111)
 */
export function classify_image_target(wrapper) {
	if (!wrapper) {
		return 'missing';
	}
	if (wrapper.querySelector('input.forge-file-upload')) {
		return 'forge-canvas';
	}
	if (wrapper.querySelector("input[type='file']")) {
		return 'gradio-image';
	}
	return 'unsupported';
}

/**
 * Build a File in the realm of the input we are about to feed.
 * input.files must be a FileList the host frame accepts; constructing it with
 * the host's own File/DataTransfer avoids cross-realm surprises.
 */
function make_file_list(input, bytes, filename, mime) {
	for (const realm of realms_for(input)) {
		try {
			const file = new realm.File([bytes], filename, { type: mime });
			const transfer = new realm.DataTransfer();
			transfer.items.add(file);
			return transfer.files;
		} catch (e) {
			/* try the next realm */
		}
	}
	throw new Error(`${LOG_PREFIX} unable to build a FileList for ${filename}`);
}

export function data_url_to_parts(data_url) {
	const parts = data_url.split(',');
	const mime = parts[0].match(/:(.*?);/)[1];
	const binary = atob(parts[1]);
	let length = binary.length;
	const bytes = new Uint8Array(length);

	while (length--) {
		bytes[length] = binary.charCodeAt(length);
	}

	return { bytes, mime };
}

/* ------------------------------------------------------------------ */
/* ForgeCanvas                                                         */
/* ------------------------------------------------------------------ */

/**
 * ForgeCanvas' real value is not the canvas the user sees: the visible image
 * is a mirror of a hidden Gradio Textbox ("LogicalImage") that img2img reads
 * at submit time. The file input only feeds an asynchronous FileReader ->
 * Image -> updateBackgroundImageData() chain that ends in that same textbox,
 * so writing the textbox directly is both shorter and verifiable.
 *
 * The textboxes are siblings of the canvas HTML block, not children of it
 * (canvas.py gives the gr.HTML the caller's elem_id and both textboxes the
 * canvas uuid), so they are resolved from the document, keyed by uuid.
 */
export function forge_canvas_uuid(wrapper) {
	const input = wrapper && wrapper.querySelector('input.forge-file-upload');
	const from_input = input && /^imageInput_(.+)$/.exec(input.id || '');
	if (from_input) {
		return from_input[1];
	}

	const container = wrapper && wrapper.querySelector('.forge-container');
	const from_container = container && /^container_(.+)$/.exec(container.id || '');
	return from_container ? from_container[1] : null;
}

/**
 * Both LogicalImages carry the same elem_id (the canvas uuid) and are told
 * apart by class, so this cannot go through getElementById.
 */
export function forge_logical_textarea(uuid, class_name) {
	const scope = root();
	try {
		const direct = scope.querySelector(`#${uuid}.${class_name} textarea`);
		if (direct) {
			return direct;
		}
	} catch (e) {
		/* fall through to the scan */
	}

	const blocks = scope.querySelectorAll(`.${class_name}`);
	for (const block of blocks) {
		if (block.id === uuid) {
			const textarea = block.querySelector('textarea');
			if (textarea) {
				return textarea;
			}
		}
	}
	return null;
}

/**
 * What img2img will submit for this canvas.
 *
 * The textbox is the value: it is what Gradio's own binding reads and what
 * ForgeCanvas writes. Gradio's copy of it (gradio_config) is a cross-check,
 * trailing the textbox by a frame or two - it must never be able to veto a
 * transfer on its own, because a mirror that is merely slow, or that a
 * particular build does not keep live, would then fail sends that worked.
 */
export async function forge_canvas_committed_value(uuid, textarea) {
	const value = (textarea && textarea.value) || '';
	const probe = gradio_component_value(uuid, 'logical_image_background');

	if (!probe.readable) {
		return { value, source: 'canvas textbox', mirror: 'not exposed by this Gradio build' };
	}

	const read = () => gradio_component_value(uuid, 'logical_image_background').value;
	let mirror = 'agrees with the textbox';

	try {
		await wait_until(() => read() === (textarea ? textarea.value : ''), {
			timeout_ms: 2000,
			interval_ms: 25,
			description: "Gradio's value to catch up with the canvas textbox",
		});
	} catch (e) {
		const held = read();
		mirror = `DISAGREES: gradio holds ${typeof held === 'string' ? `${held.length} bytes` : typeof held}`;
	}

	return { value, source: 'canvas textbox', mirror };
}

/* ------------------------------------------------------------------ */
/* Send fingerprints                                                   */
/* ------------------------------------------------------------------ */

const SEND_LOG_LIMIT = 10;
const send_records = [];

/** Start recording a transfer. Everything it learns lands in one record. */
export function start_send_record(destination) {
	const record = {
		destination,
		startedAt: new Date().toISOString(),
		steps: [],
		outcome: 'in progress',
	};
	const started = Date.now();
	record.step = (what, detail) => {
		record.steps.push(`+${String(Date.now() - started).padStart(5)}ms  ${what}${detail ? ` - ${detail}` : ''}`);
		return record;
	};

	send_records.unshift(record);
	send_records.length = Math.min(send_records.length, SEND_LOG_LIMIT);
	return record;
}

/** Where the extension writes transfer logs, relative to the WebUI. */
export const SEND_LOG_ENDPOINT = '/minipaint/log';

/**
 * Addresses to try for that route.
 *
 * This iframe is served as "<prefix>/file=<path>/index.html", so the WebUI's
 * root is whatever precedes "/file=" - which is not "/" when the WebUI is
 * behind a reverse proxy or started with a sub-path, exactly the setups where
 * these transfers go wrong in the first place.
 */
export function send_log_endpoints() {
	const candidates = [];

	try {
		const here = new URL(window.location.href);
		const marker = here.pathname.indexOf('/file=');
		if (marker > 0) {
			candidates.push(`${here.origin}${here.pathname.slice(0, marker)}${SEND_LOG_ENDPOINT}`);
		}
	} catch (e) {
		/* fall back to the root-relative address */
	}

	candidates.push(SEND_LOG_ENDPOINT);
	return candidates.filter((address, index) => candidates.indexOf(address) === index);
}

/** The address that worked last time, so later sends do not re-probe. */
let send_log_endpoint = null;

/**
 * Hand a finished transfer to the extension, which appends it to
 * logs/send-log.txt next to the extension itself.
 *
 * A plain request rather than anything Gradio, so it still reports when what
 * failed is the Gradio round trip. Older installs have no such route; the
 * console keeps the same information either way, so this stays quiet.
 */
export async function write_send_log(record) {
	if (!record) {
		return { ok: false, reason: 'there was nothing to log' };
	}

	const body = JSON.stringify({
		destination: record.destination,
		startedAt: record.startedAt,
		outcome: record.outcome,
		steps: record.steps,
	});

	const addresses = send_log_endpoint ? [send_log_endpoint] : send_log_endpoints();
	let reason = 'no address to try';

	for (const address of addresses) {
		try {
			const response = await fetch(address, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body,
			});

			if (response.status === 404) {
				reason =
					'the extension has no log route (404) - the WebUI is running an older copy of ' +
					'this extension, or has not been restarted since it was updated';
				continue;
			}
			if (response.status === 401 || response.status === 403) {
				reason = `the WebUI refused the log request (${response.status}) - it is behind authentication`;
				continue;
			}
			if (!response.ok) {
				reason = `the log route answered ${response.status}`;
				continue;
			}

			let written = null;
			try {
				written = await response.json();
			} catch (e) {
				reason = 'the log route answered with something that is not JSON';
				continue;
			}

			if (!written || written.ok !== true) {
				reason = `the extension could not write the log: ${(written && written.error) || 'unknown reason'}`;
				continue;
			}

			send_log_endpoint = address;
			if (record.logged_path !== written.path) {
				record.logged_path = written.path;
				log_info(`transfer log written to ${written.path}`);
			}
			return written;
		} catch (e) {
			reason = `the log route could not be reached (${e && e.message ? e.message : e})`;
		}
	}

	// Worth saying out loud: a missing log is how the last few failures became
	// impossible to explain.
	log_warning(`the transfer could not be logged to a file - ${reason}`);
	return { ok: false, reason };
}

/**
 * The whole story as one block of text: what the host looks like, what each
 * destination is holding, and the last transfers step by step.
 *
 * This is what gets shown in the editor, because a console and a log file are
 * both out of reach on a phone.
 */
export async function report_text(extra) {
	const options = extra || {};
	const lines = [];
	const say = (line) => lines.push(line);

	say('miniPaint send report');
	say(`generated ${new Date().toISOString()}`);
	say(`editor: ${window.location.href}`);

	let report = null;
	try {
		report = await debug_report();
	} catch (e) {
		say(`could not inspect the WebUI: ${e && e.message ? e.message : e}`);
	}

	if (report) {
		say(`gradio: ${report.gradioVersion || 'unknown'}`);
		say(`forge canvas present: ${report.hasForgeCanvas}, gradioApp(): ${report.hasGradioApp}`);
		say(
			`img2img mode the WebUI will use: ${report.img2imgMode.valueTheWebUIWillUse}` +
			` (visible sub-tab: ${report.img2imgMode.visibleSubTab})`
		);
		say('');
		say('destinations:');
		for (const selector of Object.keys(report.targets)) {
			const target = report.targets[selector];
			if (typeof target === 'string') {
				say(`  ${selector}: ${target}`);
			} else {
				say(
					`  ${selector}: ${target.kind}, canvas ${target.uuid}, background ${target.background}, ` +
					`foreground ${target.foreground}`
				);
				say(`      holds ${target.holds} (read from the ${target.readValueFrom})`);
			}
		}
		say('');
		say(`controlnet units: ${JSON.stringify(report.controlnetUnits)}`);
	}

	if (options.log_file) {
		say('');
		say(`log file: ${options.log_file}`);
	}

	say('');
	say(`transfers (newest first, ${send_records.length}):`);
	if (!send_records.length) {
		say('  none yet');
	}
	for (const record of send_records) {
		say('');
		say(`[${record.startedAt}] ${record.destination} -> ${record.outcome}`);
		for (const step of record.steps) {
			say(`    ${step}`);
		}
	}

	return lines.join('\n');
}

/**
 * The last few transfers, step by step, with what each one saw.
 * Exposed to the host as a1111minipaint.sendLog().
 */
export function send_log() {
	const readable = send_records.map((record) => ({
		destination: record.destination,
		startedAt: record.startedAt,
		outcome: record.outcome,
		steps: record.steps,
	}));
	console.log(`${LOG_PREFIX} last ${readable.length} transfer(s)`, readable);
	return readable;
}

/** Short description of a value, for the record. */
async function describe_value(value) {
	if (!value) {
		return 'empty';
	}
	if (!is_png_data_url(value)) {
		return `${value.length} bytes, not a PNG data URL`;
	}
	try {
		const signature = await image_signature(value);
		return (
			`${signature.width}x${signature.height}, ${signature.length} bytes, hash ${signature.hash}, ` +
			`${signature.coverage}% visible, average ${signature.mean}`
		);
	} catch (e) {
		return `${value.length} bytes that do not decode`;
	}
}

/**
 * Keep a canvas' <img> from being reloaded with what it already shows.
 *
 * ForgeCanvas re-encodes the background by doing, in one synchronous block:
 *
 *     image.src = this.img;                  // drawImage()
 *     tempCtx.drawImage(image, ...);         // updateBackgroundImageData()
 *     background.set_value(tempCanvas.toDataURL());
 *
 * Assigning src is meant to be a no-op when the element already displays that
 * exact URL ("completely available" aborts the update), and where it is, the
 * re-encode is faithful. Where it is not, the element loses its decoded frame
 * for an instant, the draw paints nothing, and the WebUI is handed a blank
 * image of the right size while the canvas visibly shows the right picture -
 * which is exactly what the reports show.
 *
 * So the redundant assignment is dropped for the length of the transfer. It
 * has no effect other than the one that breaks this, and the count of how
 * often it was dropped says whether this host needed it.
 */
function hold_image_loaded(image) {
	if (!image) {
		return () => 0;
	}

	const realm = (image.ownerDocument && image.ownerDocument.defaultView) || window;
	let descriptor = null;
	try {
		descriptor = Object.getOwnPropertyDescriptor(realm.HTMLImageElement.prototype, 'src');
	} catch (e) {
		descriptor = null;
	}
	if (!descriptor || !descriptor.set || !descriptor.get) {
		return () => 0;
	}

	let suppressed = 0;
	try {
		Object.defineProperty(image, 'src', {
			configurable: true,
			enumerable: false,
			get() {
				return descriptor.get.call(this);
			},
			set(value) {
				if (value === descriptor.get.call(this) && this.complete && this.naturalWidth > 0) {
					suppressed++;
					return;
				}
				descriptor.set.call(this, value);
			},
		});
	} catch (e) {
		return () => 0;
	}

	return () => {
		try {
			delete image.src;
		} catch (e) {
			/* the element keeps the accessor; it still behaves correctly */
		}
		return suppressed;
	};
}

/**
 * Commit an image to a ForgeCanvas, then confirm that what img2img will
 * submit really is the image we sent - same size, same picture - retrying the
 * write before giving up.
 *
 * Resolves with what was verified; rejects, after `attempts` tries, with what
 * the WebUI is holding instead.
 */
export async function set_forge_canvas_image(wrapper, data_url, options = {}) {
	const label = `#${(wrapper && wrapper.id) || '(no id)'}`;
	const attempts = options.attempts || 3;

	if (!is_png_data_url(data_url)) {
		// LogicalImage.preprocess() drops anything that is not a PNG data URL,
		// which would reach the backend as "no image" instead of an error.
		throw new Error(`${LOG_PREFIX} ${label}: ForgeCanvas only accepts PNG data URLs`);
	}

	const uuid = forge_canvas_uuid(wrapper);
	if (!uuid) {
		throw new Error(`${LOG_PREFIX} ${label}: could not read the ForgeCanvas uuid`);
	}

	const background = forge_logical_textarea(uuid, 'logical_image_background');
	if (!background) {
		throw new Error(`${LOG_PREFIX} ${label}: ForgeCanvas ${uuid} has no logical_image_background`);
	}

	const foreground = forge_logical_textarea(uuid, 'logical_image_foreground');
	// The element the canvas itself draws, which is what its re-encode is made
	// of - looked up its way, so a duplicate in the page cannot go unnoticed.
	const drawn = root().getElementById
		? root().getElementById(`image_${uuid}`)
		: query(`#image_${uuid}`);
	const visible = drawn || wrapper.querySelector('img.forge-image');
	const sent = await image_signature(data_url);
	const budget = transfer_budget(data_url);
	const record = options.record || start_send_record(label);

	const shown = wrapper.querySelector('img.forge-image');
	if (drawn && shown && drawn !== shown) {
		record.step(
			'the canvas draws a different element than the one on screen',
			'there is more than one canvas with this uuid in the page'
		);
	}

	// Give the canvas' element the image before the canvas asks for it, so its
	// very first re-encode has a decoded frame to draw rather than losing the
	// first attempt to a reload it did not need.
	if (visible && is_png_data_url(data_url)) {
		try {
			visible.src = data_url;
			if (typeof visible.decode === 'function') {
				await visible.decode();
			}
			record.step('primed the canvas image', 'so its first re-encode has something to draw');
		} catch (e) {
			record.step('could not prime the canvas image', (e && e.message) || String(e));
		}
	}

	const release = hold_image_loaded(visible);

	record.step(
		'exported image',
		`${sent.width}x${sent.height}, ${sent.length} bytes, hash ${sent.hash}, ` +
		`canvas ${uuid}, allowing ${budget.display_ms}ms for ${budget.megabytes}MB`
	);

	let failure = 'the transfer was never attempted';

	for (let attempt = 1; attempt <= attempts; attempt++) {
		record.step(`attempt ${attempt}`, `textbox currently ${await describe_value(background.value)}`);

		// Foreground first: a scribble/mask from the previous image survives an
		// image of identical dimensions, and would then be sent along with ours.
		if (foreground && foreground.value) {
			set_native_value(foreground, '');
			dispatch_host_event(foreground, 'input');
			record.step('cleared the foreground');
		}

		if (background.value !== data_url) {
			set_native_value(background, data_url);
			dispatch_host_event(background, 'input');
			record.step('wrote the image into the textbox', `it now holds ${background.value.length} bytes`);
		} else {
			record.step('textbox already holds the image', 'waiting for the canvas rather than rewriting');
		}

		// ForgeCanvas polls the textbox every 100ms, decodes it into its
		// visible <img>, then re-encodes that image back into the textbox. All
		// of that scales with the image, so the wait does too.
		if (visible) {
			try {
				await wait_until(
					() => visible.src === data_url && visible.complete && visible.naturalWidth > 0,
					{ timeout_ms: budget.display_ms, description: `${label} to display the sent image` }
				);
				record.step('canvas displays the image');
			} catch (e) {
				record.step('canvas never displayed the image', `waited ${budget.display_ms}ms`);
				log_warning(`${label}: ForgeCanvas did not display the image within ${budget.display_ms}ms`);
			}
		}

		// The canvas rewrites the textbox after it loads. Judging before that
		// has stopped moving reads a value that is still on its way.
		try {
			let previous = background.value;
			await wait_until(
				() => {
					const settled = background.value === previous;
					previous = background.value;
					return settled;
				},
				{ timeout_ms: budget.settle_ms, interval_ms: 100, stable_ms: 400, description: `${label} to settle` }
			);
		} catch (e) {
			record.step('the textbox never stopped changing', `waited ${budget.settle_ms}ms`);
		}

		if (!background.isConnected) {
			release();
			record.outcome = 'failed: the component was replaced mid-transfer';
			throw new Error(`${LOG_PREFIX} ${label}: ForgeCanvas ${uuid} was replaced during the transfer`);
		}

		const committed = await forge_canvas_committed_value(uuid, background);
		record.step(
			'read back what the WebUI will submit',
			`${await describe_value(committed.value)} (from the ${committed.source}; gradio copy ${committed.mirror})`
		);

		// The canvas re-encodes by drawing its own <img> into a canvas, so
		// when the result is wrong, its size and the state of that <img> are
		// what tells us why.
		const container = wrapper.querySelector('.forge-container');
		record.step(
			'canvas state',
			`container ${container ? `${container.clientWidth}x${container.clientHeight}` : 'missing'}` +
			(visible
				? `, image natural ${visible.naturalWidth}x${visible.naturalHeight}, complete ${visible.complete}` +
				  `, showing ${visible.src === data_url ? 'the sent image' : `${(visible.src || '').slice(0, 24)}...`}`
				: ', no image element')
		);

		if (!committed.value) {
			failure = 'the WebUI holds no image for it';
		} else if (!is_png_data_url(committed.value)) {
			failure = 'the WebUI holds something that is not a PNG';
		} else {
			let held = null;
			try {
				held = await image_signature(committed.value);
			} catch (e) {
				failure = `the WebUI holds ${committed.value.length} bytes that do not decode as an image`;
			}

			if (held) {
				const comparison = compare_signatures(sent, held);
				if (comparison.same) {
					const how = comparison.byte_identical
						? 'byte-identical'
						: `re-encoded by the host, pixel difference ${comparison.difference}/255`;
					const dropped = release();
					record.step(
						'verified',
						`${how}, on attempt ${attempt}` +
						(dropped ? `, after dropping ${dropped} redundant reload(s) of the canvas image` : '')
					);
					record.outcome = `sent: ${sent.width}x${sent.height}, ${how}`;
					log_info(`${label} holds the sent image: ${held.width}x${held.height}, ${how}`);

					return {
						kind: 'forge-canvas',
						uuid,
						width: sent.width,
						height: sent.height,
						attempt,
						record,
						verified_against: committed.source,
						byte_identical: comparison.byte_identical,
						pixel_difference: comparison.difference,
						// Re-checked just before navigating, and again after.
						still_holds: async () => {
							const now = await forge_canvas_committed_value(uuid, background);
							if (!now.value) {
								return 'the WebUI now holds no image';
							}
							try {
								const again = await image_signature(now.value);
								const check = compare_signatures(sent, again);
								return check.same ? null : `the WebUI now holds a different image: ${check.reason}`;
							} catch (e) {
								return 'the WebUI now holds something that does not decode';
							}
						},
					};
				}
				failure = `the WebUI holds a different image: ${comparison.reason}`;
				record.step('sent:', '');
				for (const row of signature_thumbnail(sent)) {
					record.step('  ', row);
				}
				record.step('the WebUI holds:', '');
				for (const row of signature_thumbnail(held)) {
					record.step('  ', row);
				}
			}
		}

		if (attempt < attempts) {
			log_warning(`${label}: ${failure} - retrying (attempt ${attempt + 1} of ${attempts})`);

			// A canvas in a tab that was never opened has no size, and cannot
			// draw itself. Bring the destination up before trying again rather
			// than repeating the same thing and expecting a different result.
			const container = wrapper.querySelector('.forge-container');
			if (typeof options.reveal === 'function' && container && !container.clientWidth) {
				options.reveal();
				record.step('revealed the destination', 'its canvas had no size to draw into');
				await pause(400);
			}
			// Only blank it when the textbox already holds exactly what we are
			// about to write: ForgeCanvas ignores a write that changes nothing,
			// but blanking otherwise throws away a load that is still running.
			if (background.value === data_url) {
				set_native_value(background, '');
				dispatch_host_event(background, 'input');
				record.step('blanked the textbox', 'so the canvas reacts to the next write');
				await pause(250);
			}
		}
	}

	const dropped = release();
	record.step('gave up', `dropped ${dropped} redundant reload(s) of the canvas image along the way`);
	record.outcome = `failed: ${failure}`;
	throw new Error(
		`${LOG_PREFIX} ${label}: sent ${sent.width}x${sent.height} (${sent.length} bytes, ` +
		`hash ${sent.hash}) ${attempts} times and ${failure}. Run a1111minipaint.sendLog() for the details.`
	);
}

/* ------------------------------------------------------------------ */
/* Ordinary gr.Image                                                   */
/* ------------------------------------------------------------------ */

/** The preview <img> a gr.Image shows once it holds a value, if any. */
function image_preview(wrapper) {
	if (!wrapper) {
		return null;
	}
	const candidates = wrapper.querySelectorAll(
		"div[data-testid='image'] img, .image-container img, .image-frame img, img"
	);
	for (const candidate of candidates) {
		const src = candidate.getAttribute('src') || '';
		if (!src || src.indexOf('data:image/svg+xml') === 0) {
			continue;
		}
		if (candidate.closest('button')) {
			continue;
		}
		return candidate;
	}
	return null;
}

function preview_source(wrapper) {
	const preview = image_preview(wrapper);
	return preview ? preview.src : '';
}

function clear_button_in(wrapper) {
	return (
		wrapper.querySelector("button[aria-label='Remove Image']") ||
		wrapper.querySelector("button[aria-label='Clear']") ||
		wrapper.querySelector("button[title='Remove Image']") ||
		wrapper.querySelector("button[title='Clear']") ||
		null
	);
}

/**
 * Commit an image to an ordinary gr.Image and wait for it to load.
 *
 * `resolve` re-reads the wrapper: clearing a Gradio image can replace the
 * component's DOM, so neither the wrapper nor the file input may be held
 * across that step.
 */
async function set_gradio_image_file(resolve, data_url, filename, record) {
	const wrapper = resolve();
	const label = `#${(wrapper && wrapper.id) || '(no id)'}`;
	const previous_source = preview_source(wrapper);
	record.step('ordinary gradio image', `preview was ${previous_source ? 'present' : 'empty'}`);

	let cleared = !previous_source;

	const clear_button = clear_button_in(wrapper);
	if (clear_button) {
		clear_button.click();

		// Let the remount settle. Knowing the old preview is gone is what
		// allows an identical image to be recognised when it comes back: on
		// Gradio 4 the same bytes upload to the same URL as last time.
		try {
			await wait_until(() => preview_source(resolve()) !== previous_source, {
				timeout_ms: 1000,
				interval_ms: 25,
				description: `${label} to clear`,
			});
			cleared = true;
		} catch (e) {
			/* not fatal: the load check below then insists on a new source */
		}
	}

	const current = resolve();
	if (!current) {
		throw new Error(`${LOG_PREFIX} ${label} disappeared while it was being cleared`);
	}

	const input = current.querySelector("input[type='file']");
	if (!input) {
		throw new Error(`${LOG_PREFIX} ${label}: no upload input after clearing`);
	}

	const { bytes, mime } = data_url_to_parts(data_url);

	input.value = '';
	input.files = make_file_list(input, bytes, filename, mime);

	// gr.Image's Upload component listens on "change"; some builds also react
	// to "input". Dispatch both so we do not depend on which one is wired.
	dispatch_host_event(input, 'input', { bubbles: true, composed: true });
	dispatch_host_event(input, 'change', { bubbles: true, composed: true });
	record.step('handed the file to the upload input', `${data_url.length} bytes`);

	// Gradio 4 uploads through the server before it has a value at all, so the
	// only honest completion signal is a preview that finished loading and
	// cannot be the one that was there before.
	await wait_until(
		() => {
			const preview = image_preview(resolve());
			return (
				!!preview &&
				(cleared || preview.src !== previous_source) &&
				preview.complete &&
				preview.naturalWidth > 0
			);
		},
		{ description: `${label} to load the sent image` }
	);

	// The preview is drawn from the component's value here, but check the
	// value the WebUI would submit as well when this build exposes it.
	const wrapper_id = (resolve() || {}).id;
	const state = wrapper_id ? gradio_component_value(wrapper_id) : { readable: false };
	if (state.readable) {
		try {
			await wait_until(
				() => {
					const value = gradio_component_value(wrapper_id).value;
					return !!value && (typeof value !== 'object' || !!(value.path || value.url));
				},
				{ timeout_ms: UI_ACK_TIMEOUT_MS, description: `${label} to hold an uploaded file` }
			);
		} catch (e) {
			throw new Error(`${LOG_PREFIX} ${label} shows the image but the WebUI holds no file for it`);
		}
	}

	record.step('verified', `against the ${state.readable ? 'gradio value' : 'preview'}`);
	record.outcome = 'sent';
	log_info(`${label} holds the sent image, verified against the ${state.readable ? 'gradio value' : 'preview'}`);

	return {
		kind: 'gradio-image',
		record,
		verified_against: state.readable ? 'gradio value' : 'preview',
		still_holds: async () => {
			if (state.readable) {
				const value = gradio_component_value(wrapper_id).value;
				if (!value || (typeof value === 'object' && !value.path && !value.url)) {
					return 'the WebUI now holds no file for it';
				}
				return null;
			}
			const preview = image_preview(resolve());
			return preview && preview.naturalWidth > 0 ? null : 'the component no longer shows the image';
		},
	};
}

/* ------------------------------------------------------------------ */
/* Transfer entry points                                               */
/* ------------------------------------------------------------------ */

/**
 * Push an image into a host image component and wait until the component's
 * real backing value holds it.
 *
 * `target` is an element or a selector; pass `options.selector` alongside an
 * element so the component can be re-resolved if the host remounts it.
 */
export async function set_image_file(target, data_url, options = {}) {
	const selector = options.selector || (typeof target === 'string' ? target : null);
	const filename = options.filename || 'image.png';

	const resolve = () => (selector ? query(selector) : null) || (typeof target === 'string' ? null : target);

	const wrapper = resolve();
	const kind = classify_image_target(wrapper);
	const label = selector || `#${(wrapper && wrapper.id) || '(no id)'}`;

	if (kind === 'missing') {
		throw new Error(`${LOG_PREFIX} destination ${label} was not found in the WebUI`);
	}
	if (kind === 'unsupported') {
		throw new Error(`${LOG_PREFIX} ${label} exists but no upload input was found`);
	}
	if (kind === 'forge-canvas') {
		return set_forge_canvas_image(wrapper, data_url, options);
	}
	return set_gradio_image_file(resolve, data_url, filename, options.record || start_send_record(label));
}

export async function set_image_on_target(selector, data_url, options = {}) {
	return set_image_file(selector, data_url, Object.assign({ selector }, options));
}

/* ------------------------------------------------------------------ */
/* img2img mode                                                        */
/* ------------------------------------------------------------------ */

/**
 * Which img2img sub-tab owns each destination.
 *
 * img2img does not read the canvas the user is looking at: it reads the slot
 * named by a hidden Number inside #mode_img2img, and that Number is only
 * updated by a *server round trip* when a sub-tab is selected (ui.py wires
 * `tab.select(fn=lambda tabnum: tabnum, outputs=[img2img_selected_tab])`, and
 * submit_img2img() does not overwrite it from the DOM). Clicking the tab and
 * generating straight away therefore generates from the previous slot - the
 * image is visibly there and still not used.
 */
export const IMG2IMG_MODES = {
	img2img_img2img: { index: 0, button: '#img2img_img2img_tab-button', label: 'img2img' },
	img2img_inpaint: { index: 2, button: '#img2img_inpaint_tab-button', label: 'inpaint' },
};

/**
 * The hidden Number holding the selected img2img sub-tab.
 * The tab strip itself lives inside the img2img TabItem, so "not inside a
 * .tabitem" has to be judged relative to the strip, not to the document.
 */
export function img2img_mode_input() {
	const container = query('#mode_img2img');
	if (!container) {
		return null;
	}
	for (const input of container.querySelectorAll("input[type='number']")) {
		const item = input.closest('.tabitem');
		if (!item || !container.contains(item)) {
			return input;
		}
	}
	return null;
}

/**
 * Select the sub-tab that owns `destination` and wait until the WebUI agrees.
 *
 * Runs while miniPaint is still the visible tab, so the round trip is over
 * before the user can reach Generate. Resolves with what happened; rejects if
 * the WebUI never acknowledged the sub-tab, because generating in that state
 * would silently use a different image slot.
 */
export async function select_img2img_mode(destination) {
	const mode = IMG2IMG_MODES[destination];
	if (!mode) {
		return { applied: false, reason: 'destination has no img2img sub-tab' };
	}

	const container = query('#mode_img2img');
	if (!container) {
		return { applied: false, reason: '#mode_img2img not found' };
	}

	const button =
		query(mode.button) || container.querySelectorAll('.tab-nav button')[mode.index] || null;
	if (!button) {
		return { applied: false, reason: `sub-tab button for ${mode.label} not found` };
	}

	const already_selected = button.classList.contains('selected');
	if (!already_selected) {
		button.click();
	}

	const input = img2img_mode_input();
	if (!input) {
		// Hosts that read the tab index in JS at submit time (upstream A1111)
		// have nothing to synchronise; the click above is all that is needed.
		return { applied: true, verified: false, reason: 'no mode value exposed in the DOM' };
	}

	const settled = () => Number(input.value) === mode.index;
	const half = Math.round(TRANSFER_TIMEOUT_MS / 2);
	const wait_for_mode = () =>
		wait_until(settled, {
			timeout_ms: half,
			// Replies to earlier tab clicks can still be in flight and would
			// overwrite a value we accepted a moment too early.
			stable_ms: 400,
			description: `the WebUI to switch img2img to ${mode.label}`,
		});

	try {
		await wait_for_mode();
	} catch (first) {
		// Two tab clicks inside one frame are collapsed by the front end into
		// no net change, so no select fires and the value stays stale for
		// good. Re-select by way of a sibling tab, letting each click land
		// before the next one, which forces the event the WebUI missed.
		try {
			const buttons = container.querySelectorAll('.tab-nav button');
			const sibling = buttons[mode.index === 0 ? 1 : 0];
			if (sibling && sibling !== button) {
				sibling.click();
				await wait_until(() => sibling.classList.contains('selected'), {
					timeout_ms: 1000,
					interval_ms: 25,
					description: 'the img2img tab strip to settle',
				});
			}
			button.click();
			await wait_for_mode();
		} catch (second) {
			throw new Error(
				`${LOG_PREFIX} the image was sent, but the WebUI still reports img2img mode ` +
				`${input.value} instead of ${mode.index} (${mode.label}); generating now would ` +
				`use a different image. Click the ${mode.label} tab in img2img once to sync it.`
			);
		}
		return { applied: true, verified: true, index: mode.index, clicked: true, retried: true };
	}

	return { applied: true, verified: true, index: mode.index, clicked: !already_selected };
}

/* ------------------------------------------------------------------ */
/* ControlNet                                                          */
/* ------------------------------------------------------------------ */

export function controlnet_image_selector(type, index) {
	return `#${type}_controlnet_ControlNet-${index}_input_image`;
}

export function controlnet_independent_selector(type, index) {
	return `#${type}_controlnet_ControlNet-${index}_controlnet_same_img2img_checkbox`;
}

/** Best-effort accordion open, only used when a unit is genuinely absent. */
export function open_controlnet_accordion(type) {
	const group = query(`#${type}_controlnet`);
	const accordion = (group && group.querySelector('#controlnet')) || query('#controlnet');
	if (!accordion) {
		return false;
	}
	const label = accordion.querySelector('button.label-wrap') || accordion.querySelector('button');
	if (!label) {
		return false;
	}
	label.click();
	return true;
}

/**
 * img2img ControlNet units ignore their own image unless "Upload independent
 * control image" is ticked - without this the unit silently uses the main
 * img2img input instead of what we just sent.
 */
export function enable_controlnet_independent_image(index) {
	const wrapper = query(controlnet_independent_selector('img2img', index));
	if (!wrapper) {
		return false;
	}
	const checkbox = wrapper.querySelector("input[type='checkbox']");
	if (!checkbox) {
		return false;
	}
	if (!checkbox.checked) {
		// click() lets the framework observe the state change it expects.
		checkbox.click();
	}
	return checkbox.checked;
}

export async function resolve_controlnet_target(type, index) {
	const selector = controlnet_image_selector(type, index);
	const existing = query(selector);
	if (existing) {
		return existing;
	}

	open_controlnet_accordion(type);
	try {
		return await wait_for_selector(selector, TRANSFER_TIMEOUT_MS);
	} catch (e) {
		throw new Error(
			`${LOG_PREFIX} ControlNet unit ${index} (${type}) was not mounted within ${TRANSFER_TIMEOUT_MS}ms`
		);
	}
}

/* ------------------------------------------------------------------ */
/* Tabs                                                                */
/* ------------------------------------------------------------------ */

function call_host(name) {
	const parent_window = host_window();
	try {
		if (parent_window && typeof parent_window[name] === 'function') {
			parent_window[name]();
			return true;
		}
	} catch (e) {
		log_error(`failed calling ${name}()`, e);
	}
	return false;
}

export const switch_to_txt2img = () => call_host('switch_to_txt2img');
export const switch_to_img2img = () => call_host('switch_to_img2img');
export const switch_to_inpaint = () => call_host('switch_to_inpaint');
export const switch_to_extras = () => call_host('switch_to_extras');

/**
 * Focus the Mini Paint tab.
 * Gradio 4 derives the tab button id from the TabItem elem_id, so
 * "#tab_minipaint-button" is stable and independent of the visible label.
 */
export function switch_to_minipaint() {
	const by_id = query('#tab_minipaint-button');
	if (by_id) {
		by_id.click();
		return;
	}

	const by_aria = query('#tabs button[aria-controls="tab_minipaint"]');
	if (by_aria) {
		by_aria.click();
		return;
	}

	const tabs = query('#tabs');
	const button = Array.from((tabs && tabs.querySelectorAll('button')) || []).find(
		(candidate) => (candidate.textContent || '').trim() === 'Mini Paint'
	);

	if (!button) {
		throw new Error(`${LOG_PREFIX} top-level Mini Paint tab button not found`);
	}
	button.click();
}

/* ------------------------------------------------------------------ */
/* Galleries                                                           */
/* ------------------------------------------------------------------ */

/**
 * The image the user is actually looking at.
 * Forge Neo renders output galleries with preview=True, so the .preview <img>
 * always mirrors the selected thumbnail. Fallbacks cover galleries configured
 * without preview.
 */
export function get_selected_gallery_image(gallery) {
	if (!gallery) {
		return null;
	}
	return (
		gallery.querySelector('.preview img') ||
		gallery.querySelector('.thumbnail-item.selected img') ||
		gallery.querySelector('img') ||
		null
	);
}

/* ------------------------------------------------------------------ */
/* Diagnostics                                                         */
/* ------------------------------------------------------------------ */

export async function debug_report() {
	const parent_window = host_window();
	const selectors = [
		'#img2img_image',
		'#img2maskimg',
		'#extras_image',
		'#txt2img_gallery',
		'#img2img_gallery',
		'#extras_gallery',
		'#image_buttons_txt2img',
		'#image_buttons_img2img',
		'#image_buttons_extras',
		'#tab_minipaint-button',
		'#controlnet',
	];

	const targets = {};
	for (const selector of selectors) {
		const element = query(selector);
		if (!element) {
			targets[selector] = 'MISSING';
			continue;
		}

		const kind = classify_image_target(element);
		if (kind !== 'forge-canvas') {
			targets[selector] = kind;
			continue;
		}

		// For ForgeCanvas the backing value is what a transfer writes and what
		// the WebUI submits, so report what it currently holds.
		const uuid = forge_canvas_uuid(element);
		const textarea = uuid && forge_logical_textarea(uuid, 'logical_image_background');
		const committed = uuid
			? await forge_canvas_committed_value(uuid, textarea)
			: { value: '', source: 'n/a' };

		let holds = 'EMPTY';
		if (committed.value) {
			try {
				const signature = await image_signature(committed.value);
				holds = `${signature.width}x${signature.height}, ${signature.length} bytes, hash ${signature.hash}`;
			} catch (e) {
				holds = `${committed.value.length} bytes that do not decode`;
			}
		}

		targets[selector] = {
			kind,
			uuid: uuid || 'UNKNOWN',
			background: textarea ? 'present' : 'MISSING',
			foreground: uuid && forge_logical_textarea(uuid, 'logical_image_foreground') ? 'present' : 'MISSING',
			readValueFrom: committed.source,
			holds,
		};
	}

	let gradio_version = null;
	try {
		gradio_version = (parent_window && parent_window.gradio_config && parent_window.gradio_config.version) || null;
	} catch (e) {
		/* ignore */
	}

	const controlnet_units = {};
	for (const type of ['txt2img', 'img2img']) {
		controlnet_units[type] = Array.from({ length: 10 }, (_, i) => i).filter((i) =>
			query(controlnet_image_selector(type, i))
		);
	}

	// Which slot img2img will actually read, and which sub-tab is on screen.
	// A mismatch is why a visibly-loaded image can go unused.
	const mode_input = img2img_mode_input();
	const selected_tab = query('#mode_img2img .tab-nav button.selected');

	const report = {
		gradioVersion: gradio_version,
		hasForgeCanvas: !!query('.forge-container .forge-file-upload'),
		hasGradioApp: !!(parent_window && typeof parent_window.gradioApp === 'function'),
		iframeSrc: window.location.href,
		img2imgMode: {
			valueTheWebUIWillUse: mode_input ? mode_input.value : 'NOT EXPOSED',
			visibleSubTab: selected_tab ? (selected_tab.textContent || '').trim() : 'UNKNOWN',
		},
		targets,
		controlnetUnits: controlnet_units,
	};

	console.log(`${LOG_PREFIX} compatibility report`, report);
	return report;
}
