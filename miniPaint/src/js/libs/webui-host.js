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
 * Build a File in the *parent* realm when possible.
 * input.files must be a FileList the host frame accepts; constructing it with
 * the parent's own File/DataTransfer avoids cross-realm surprises.
 */
function make_file_list(bytes, filename, mime) {
	const parent_window = host_window();
	const candidates = parent_window ? [parent_window, window] : [window];

	for (const realm of candidates) {
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

/**
 * Push an image into a host image component.
 * Works for both ForgeCanvas and ordinary gr.Image because it feeds the file
 * input that each of them listens on.
 */
export function set_image_file(wrapper, data_url, filename = 'image.png') {
	const kind = classify_image_target(wrapper);

	if (kind === 'missing') {
		throw new Error(`${LOG_PREFIX} target wrapper not found`);
	}
	if (kind === 'unsupported') {
		throw new Error(
			`${LOG_PREFIX} #${wrapper.id || '(no id)'} exists but no upload input was found`
		);
	}

	const input =
		wrapper.querySelector("input.forge-file-upload[type='file']") ||
		wrapper.querySelector("input[type='file']");

	// Clear whatever is loaded first. On ForgeCanvas this also wipes leftover
	// scribbles, which loading a same-sized image would otherwise keep.
	const clear_button =
		wrapper.querySelector('button.forge-btn[title="Remove"]') ||
		wrapper.querySelector("button[aria-label='Remove Image']") ||
		wrapper.querySelector("button[aria-label='Clear']");

	if (clear_button) {
		clear_button.click();
	}

	const { bytes, mime } = data_url_to_parts(data_url);

	input.value = '';
	input.files = make_file_list(bytes, filename, mime);

	// ForgeCanvas listens on "change"; gr.Image's Upload component accepts
	// either. Dispatch both so we do not depend on which one is wired.
	input.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
	input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
}

export function set_image_on_target(selector, data_url, filename) {
	const wrapper = query(selector);
	if (!wrapper) {
		throw new Error(`${LOG_PREFIX} destination ${selector} was not found in the WebUI`);
	}
	set_image_file(wrapper, data_url, filename);
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
	if (!checkbox || checkbox.checked) {
		return false;
	}
	// click() lets the framework observe the state change it expects.
	checkbox.click();
	return true;
}

export async function resolve_controlnet_target(type, index) {
	const selector = controlnet_image_selector(type, index);
	const existing = query(selector);
	if (existing) {
		return existing;
	}

	open_controlnet_accordion(type);
	try {
		return await wait_for_selector(selector, 10000);
	} catch (e) {
		throw new Error(`${LOG_PREFIX} ControlNet unit ${index} (${type}) was not mounted within 10s`);
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
		targets[selector] = element ? classify_image_target(element) || 'present' : 'MISSING';
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
