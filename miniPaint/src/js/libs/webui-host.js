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

const LOG_PREFIX = 'MiniPaint:';

/** How long a transfer may take before it is reported as failed. */
export const TRANSFER_TIMEOUT_MS = 10000;

/**
 * How long to wait for a component to *display* what we committed. This is a
 * consistency check on top of the backing value, not the success criterion,
 * so it is deliberately shorter than TRANSFER_TIMEOUT_MS.
 */
const UI_ACK_TIMEOUT_MS = 3000;

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

/**
 * Poll a synchronous predicate until it is truthy.
 * Everything we wait on is host state we do not own (Gradio's uploader,
 * ForgeCanvas' 100ms textarea poll), so this is bounded by a deadline rather
 * than trusting a single event.
 */
export function wait_until(predicate, options = {}) {
	const timeout_ms = options.timeout_ms || TRANSFER_TIMEOUT_MS;
	const interval_ms = options.interval_ms || 50;
	const description = options.description || 'condition';

	return new Promise((resolve, reject) => {
		const deadline = Date.now() + timeout_ms;

		const tick = () => {
			let value;
			try {
				value = predicate();
			} catch (e) {
				reject(e);
				return;
			}
			if (value) {
				resolve(value);
				return;
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

/** Intrinsic size of an exported data URL, used to verify what landed. */
function decode_image_size(data_url) {
	return new Promise((resolve, reject) => {
		const image = new Image();
		image.onload = () => resolve({ width: image.naturalWidth, height: image.naturalHeight });
		image.onerror = () => reject(new Error(`${LOG_PREFIX} the exported image could not be decoded`));
		image.src = data_url;
	});
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
 * Commit an image to a ForgeCanvas and wait until its backing value holds it.
 *
 * Resolves once logical_image_background is the image we sent; rejects on a
 * missing canvas, a non-PNG export, or a value that did not stick.
 */
export async function set_forge_canvas_image(wrapper, data_url) {
	const label = `#${(wrapper && wrapper.id) || '(no id)'}`;

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

	const size = await decode_image_size(data_url);

	// Foreground first: a scribble/mask from the previous image survives an
	// image of identical dimensions, and would then be sent along with ours.
	const foreground = forge_logical_textarea(uuid, 'logical_image_foreground');
	if (foreground && foreground.value) {
		set_native_value(foreground, '');
		dispatch_host_event(foreground, 'input');
	}

	set_native_value(background, data_url);
	dispatch_host_event(background, 'input');

	if (background.value !== data_url) {
		throw new Error(`${LOG_PREFIX} ${label}: logical_image_background rejected the image`);
	}

	// ForgeCanvas polls the textbox every 100ms, then mirrors it into the
	// visible <img> as-is. Waiting for that is how we know the canvas agreed
	// with us; it is not what img2img reads, so a timeout is not fatal.
	const visible = wrapper.querySelector('img.forge-image');
	let acknowledged = false;

	if (visible) {
		try {
			await wait_until(() => visible.src === data_url && visible.complete && visible.naturalWidth > 0, {
				timeout_ms: UI_ACK_TIMEOUT_MS,
				description: `${label} to display the sent image`,
			});
			acknowledged = true;
		} catch (e) {
			log_warning(`${label}: ForgeCanvas did not display the image within ${UI_ACK_TIMEOUT_MS}ms`);
		}
	}

	// A component that was remounted while we were writing to it would leave
	// us verifying a detached textarea that the WebUI no longer reads.
	if (!background.isConnected) {
		throw new Error(`${LOG_PREFIX} ${label}: ForgeCanvas ${uuid} was replaced during the transfer`);
	}

	// Once the canvas has loaded the image it re-encodes it from its own
	// <img> and writes that back, so the committed string is allowed to
	// differ from ours - but only after we saw the canvas take our image.
	const committed = background.value;
	if (committed !== data_url && !(acknowledged && is_png_data_url(committed))) {
		throw new Error(`${LOG_PREFIX} ${label}: logical_image_background no longer holds the sent image`);
	}

	return { kind: 'forge-canvas', uuid, width: size.width, height: size.height };
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
async function set_gradio_image_file(resolve, data_url, filename) {
	const wrapper = resolve();
	const label = `#${(wrapper && wrapper.id) || '(no id)'}`;
	const previous_source = preview_source(wrapper);

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

	return { kind: 'gradio-image' };
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
		return set_forge_canvas_image(wrapper, data_url);
	}
	return set_gradio_image_file(resolve, data_url, filename);
}

export async function set_image_on_target(selector, data_url, options = {}) {
	return set_image_file(selector, data_url, Object.assign({ selector }, options));
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

export function debug_report() {
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

		// For ForgeCanvas the backing textbox is what a transfer writes, so
		// report whether it can be resolved rather than just the canvas.
		const uuid = forge_canvas_uuid(element);
		targets[selector] = {
			kind,
			uuid: uuid || 'UNKNOWN',
			background: uuid && forge_logical_textarea(uuid, 'logical_image_background') ? 'present' : 'MISSING',
			foreground: uuid && forge_logical_textarea(uuid, 'logical_image_foreground') ? 'present' : 'MISSING',
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

	const report = {
		gradioVersion: gradio_version,
		hasForgeCanvas: !!query('.forge-container .forge-file-upload'),
		hasGradioApp: !!(parent_window && typeof parent_window.gradioApp === 'function'),
		iframeSrc: window.location.href,
		targets,
		controlnetUnits: controlnet_units,
	};

	console.log(`${LOG_PREFIX} compatibility report`, report);
	return report;
}
