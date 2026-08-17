import alertify from './../../../../node_modules/alertifyjs/build/alertify.min.js';
import app from './../../app.js';
import config from './../../config.js';
import Base_layers_class from './../../core/base-layers.js';
import Dialog_class from './../../libs/popup.js';
import Tools_settings_class from "../tools/settings";
import File_save_class from './save.js';
import File_open_class from './open.js';
import Helper_class from './../../libs/helpers.js';
import * as Host from './../../libs/webui-host.js';

var instance = null;

/**
 * manages sending files to other tabs
 */
class File_send_class {
	constructor() {
		//singleton
		if (instance) {
			return instance;
		}
		instance = this;

		this.Base_layers = new Base_layers_class();
		this.Helper = new Helper_class();
		this.POP = new Dialog_class();
		this.Tools_settings = new Tools_settings_class();
		this.Saver = new File_save_class();
		this.Loader = new File_open_class();
		this.Helper = new Helper_class();

		// Outbound transfers run one at a time; see send().
		this.pending_send = Promise.resolve();
		this.send_token = 0;

		this.register_bridge();
	}

	/**
	 * Expose the hooks the WebUI extension calls into.
	 * Never let a missing or changed host break module loading: this constructor
	 * runs from Base_gui.load_modules(), so throwing here takes down all of
	 * miniPaint, not just the send feature.
	 */
	register_bridge() {
		try {
			const parent_window = Host.host_window();
			const bridge = parent_window && parent_window.a1111minipaint;
			if (!bridge) {
				return;
			}
			bridge.recieve = this.recieveImage.bind(this);
			bridge.createSendButton = this.createSendToMiniPaintButton.bind(this);
			bridge.debugReport = Host.debug_report;
			bridge.sendLog = Host.send_log;
		} catch (e) {
			Host.log_error('could not connect to the WebUI bridge', e);
		}
	}

	dataURLtoFile(dataurl, filename) {
		const { bytes, mime } = Host.data_url_to_parts(dataurl);
		return new File([bytes], filename, { type: mime });
	}

	/**
	 * Run one outbound transfer at a time and navigate only afterwards.
	 *
	 * `transfer` resolves with the function that focuses its destination, and
	 * that function is called only if no newer send has been started since:
	 * a slow transfer must never drag the user to its own tab after the user
	 * has already sent something else.
	 *
	 * Resolves true when the destination's backing value holds the image, and
	 * false when the transfer failed - never before the image has landed.
	 */
	send(description, transfer) {
		const token = ++this.send_token;

		const task = this.pending_send
			.catch(() => { })
			.then(async () => {
				// One record per send, written to the extension's log file
				// whichever way the transfer ends.
				const record = Host.start_send_record(description);
				let failure = null;
				try {
					const navigate = await transfer(record);
					if (typeof navigate === 'function' && token === this.send_token) {
						navigate();
					}
				} catch (e) {
					Host.log_error(`sending to ${description} failed`, e);
					failure = this.failure_reason(e);
				}

				// Report only once the log has been written, so the message can
				// say where to read the details - or why they are not there.
				const logged = await Host.write_send_log(record);

				if (failure) {
					this.report_failure(description, failure, logged);
					return false;
				}
				this.report_success(description);
				return true;
			});

		this.pending_send = task;
		return task;
	}

	/**
	 * Say what happened, in the editor and in the console.
	 *
	 * A toast that throws must never be able to turn a reported outcome into a
	 * silent one - that is indistinguishable from the bug this code exists to
	 * catch - so the console line comes first and the toast is best effort.
	 */
	report_success(description) {
		Host.log_info(`sent the image to ${description}`);
		try {
			alertify.success(`Image sent to ${description}.`);
		} catch (e) {
			Host.log_warning('could not show the success message', e);
		}
	}

	report_failure(description, reason, logged) {
		const where = this.where_to_read_more(logged);
		Host.log_error(`could not send the image to ${description}: ${reason}`);
		try {
			alertify.error(`Could not send the image to ${description}: ${reason} ${where}`);
		} catch (e) {
			Host.log_warning('could not show the failure message', e);
		}
	}

	/**
	 * Where the details of a failure ended up.
	 *
	 * When the log could not be written that is itself worth saying: an
	 * instruction to read a file that was never created is how the last round
	 * of failures stayed unexplained.
	 */
	where_to_read_more(logged) {
		if (logged && logged.ok && logged.path) {
			return `Details: ${logged.path}`;
		}
		if (logged && logged.reason) {
			return `Details are in the browser console - no log file was written because ${logged.reason}.`;
		}
		return 'Details are in the browser console.';
	}

	/** The part of a transfer error that is worth putting in a toast. */
	failure_reason(error) {
		const message = (error && error.message) || String(error);
		return message.replace(/^MiniPaint:\s*/, '');
	}

	/**
	 * Resolve a destination and commit the image to it.
	 *
	 * The tab is opened only when the destination is not mounted yet: while it
	 * is already there, the user stays in miniPaint for the whole commit and
	 * so cannot reach the destination's Generate button too early.
	 */
	async commit_to_destination(record, selector, switch_to, image_data_url, options = {}) {
		const { wrapper, opened } = await Host.resolve_target(selector, switch_to);
		let committed;

		try {
			committed = await Host.set_image_file(wrapper, image_data_url, { selector, record });
		} catch (e) {
			if (opened) {
				Host.log_warning(
					`${selector} had to be opened before it could be resolved, so its tab is now in ` +
					'front without having received the image'
				);
			}
			throw e;
		}

		// img2img generates from whichever slot its own mode value names, not
		// from the canvas on screen, and that value only changes on a server
		// round trip. Settle it here, while miniPaint is still in front, so
		// the image that just landed is the one Generate will use.
		if (options.img2img_destination) {
			const mode = await Host.select_img2img_mode(options.img2img_destination);
			record.step('img2img sub-tab', JSON.stringify(mode));
			if (mode.applied && !mode.verified) {
				Host.log_warning(`could not confirm the img2img sub-tab: ${mode.reason}`);
			}
		}

		// Everything above can be undone by work the host had already started:
		// a canvas load still running, a reply still on its way. Look once more
		// at the last possible moment rather than trusting an older reading.
		await this.confirm_still_held(committed, selector, record);
		this.watch_after_send(committed, selector, record);

		return switch_to;
	}

	/** Re-read the destination immediately before the user is sent to it. */
	async confirm_still_held(committed, selector, record) {
		if (!committed || typeof committed.still_holds !== 'function') {
			return;
		}
		const problem = await committed.still_holds();
		record.step('final check before switching tabs', problem || 'still holds the sent image');
		if (problem) {
			record.outcome = `failed: ${problem}`;
			throw new Error(`${Host.LOG_PREFIX} ${selector}: ${problem}`);
		}
	}

	/**
	 * Look again a few seconds after the send.
	 *
	 * Nothing can be rolled back by then - the point is that a value quietly
	 * replaced after a successful send gets said out loud, instead of only
	 * showing up as a generation that ignored the image.
	 */
	watch_after_send(committed, selector, record) {
		if (!committed || typeof committed.still_holds !== 'function') {
			return;
		}
		setTimeout(async () => {
			try {
				const problem = await committed.still_holds();
				if (problem) {
					record.outcome = `sent, then lost it: ${problem}`;
					record.step('after the send', problem);
					const logged = await Host.write_send_log(record);
					this.report_failure(
						selector,
						`${problem}. The image was sent but the WebUI dropped it afterwards - send it again`,
						logged
					);
				}
			} catch (e) {
				Host.log_warning(`could not re-check ${selector} after the send`, e);
			}
		}, 3000);
	}

	sendImageCanvasEditor(type) {
		const name = type === 'img2img_inpaint' ? 'Inpaint' : 'img2img';

		return this.send(name, async (record) => {
			const selector = Host.DESTINATIONS[type];
			if (!selector) {
				throw new Error(`MiniPaint: unknown img2img destination "${type}"`);
			}

			const switch_to =
				type === 'img2img_inpaint' ? Host.switch_to_inpaint : Host.switch_to_img2img;

			const image_data_url = await this.Saver.export_data_url();
			return this.commit_to_destination(record, selector, switch_to, image_data_url, {
				img2img_destination: type,
			});
		});
	}

	sendImageCanvasEditorControlNet(type, index) {
		return this.send(`${type} ControlNet unit ${index}`, async (record) => {
			const switch_to = type === 'txt2img' ? Host.switch_to_txt2img : Host.switch_to_img2img;
			const selector = Host.controlnet_image_selector(type, index);

			const image_data_url = await this.Saver.export_data_url();
			const wrapper = await Host.resolve_controlnet_target(type, index);

			// Must happen before the image lands, otherwise the unit keeps
			// using the main img2img image instead of ours.
			if (type === 'img2img' && !Host.enable_controlnet_independent_image(index)) {
				Host.log_warning(
					`ControlNet unit ${index} has no "Upload independent control image" checkbox; ` +
					'the unit may use the img2img input instead of the image sent'
				);
			}

			const committed = await Host.set_image_file(wrapper, image_data_url, { selector, record });
			await this.confirm_still_held(committed, selector, record);
			this.watch_after_send(committed, selector, record);

			return switch_to;
		});
	}

	GUISendExtras() {
		return this.send('Extras', async (record) => {
			const image_data_url = await this.Saver.export_data_url();
			return this.commit_to_destination(
				record,
				Host.DESTINATIONS.extras,
				Host.switch_to_extras,
				image_data_url
			);
		});
	}

	GUISendControlnet() {
		let maxModelAmount = 3;
		const counter = Host.query('#a1111minipaint_controlnet_max');
		const field = counter && counter.querySelector('textarea');
		if (field) {
			const parsed = Number(field.value);
			if (Number.isFinite(parsed) && parsed > 0) {
				maxModelAmount = parsed;
			}
		}

		let modelSelector = []
		for (let i = 0; i < maxModelAmount; i++) {
			modelSelector.push("Controlnet " + i)
		}
		let _this = this
		var settings = {
			title: "Send Image to Controlnet",
			params: [
				{ name: "txt2img", titel: "Type", values: ["Text to Image", "Image to Image"] },
				{ name: "cnn", titel: "Number", values: modelSelector }
			],
			on_finish: function (params) {
				_this.sendImageCanvasEditorControlNet(
					params.txt2img === "Text to Image" ? "txt2img" : "img2img",
					modelSelector.indexOf(params.cnn)
				)
			}
		};
		this.POP.show(settings);
	}

	GUISendImg2img() {
		let _this = this
		var settings = {
			title: "Send Image to Image2image",
			params: [
				{ name: "img2img", titel: "Type", values: ["Image2Image Init Image", "Image2Image Inpaint Image"] },
			],
			on_finish: function (params) {
				_this.sendImageCanvasEditor(params.img2img === "Image2Image Init Image" ? "img2img_img2img" : "img2img_inpaint")
			}
		};
		this.POP.show(settings);
	}

	/**
	 * Add a "send this output to Mini Paint" button to a WebUI output row.
	 * Safe to call repeatedly - the WebUI can reload its own UI at any time.
	 */
	createSendToMiniPaintButton(queryId, gallery) {
		const row = Host.query(`#${queryId}`);
		if (!row) {
			Host.log_error(`output button row #${queryId} was not found`);
			return;
		}

		const button_id = `${queryId}_open_in_minipaint`;
		let button = Host.query(`#${button_id}`);

		if (!button) {
			const template = row.querySelector('button');
			if (template) {
				// Shallow clone keeps the host's button styling (the scoped
				// class names are build-specific) without copying its content.
				button = template.cloneNode(false);
				button.removeAttribute('style');
				button.removeAttribute('disabled');
			} else {
				button = row.ownerDocument.createElement('button');
				button.className = 'lg secondary gradio-button tool';
			}

			button.id = button_id;
			button.textContent = '✏️';
			button.title = 'Send image to miniPaint tab.';
			button.setAttribute('aria-label', 'Send image to miniPaint tab.');

			// Land in the same container as the row's other buttons.
			const template_parent = template ? template.parentElement : row;
			(template_parent || row).appendChild(button);
		}

		// Assigning onclick replaces any previous handler, so reloading the
		// WebUI cannot stack duplicate listeners on the same button.
		button.onclick = () => this.recieveImage(gallery);
	}

	async recieveImage(gallery) {
		try {
			const target = gallery || Host.query('#txt2img_gallery');
			const img = Host.get_selected_gallery_image(target);

			if (!img) {
				Host.log_error(
					`no selected/visible image found in #${(target && target.id) || 'gallery'}`
				);
				return;
			}

			if (!img.complete || !img.naturalWidth) {
				await img.decode();
			}

			const width = img.naturalWidth;
			const height = img.naturalHeight;
			if (!width || !height) {
				Host.log_error('the selected gallery image has not finished loading');
				return;
			}

			Host.switch_to_minipaint();

			const canvas = document.createElement('canvas');
			canvas.width = width;
			canvas.height = height;
			canvas.getContext('2d').drawImage(img, 0, 0);

			new File_open_class().file_open_data_url_handler(canvas.toDataURL('image/png'));
		} catch (e) {
			Host.log_error('could not open the selected gallery image', e);
		}
	}
}
export default File_send_class
