/* Authcor customer portal -- the one shared browser helper.
 *
 * The portal is server-rendered Jinja (PHASE-7-PORTAL.md section 1); this
 * exists only so the action forms can post to the whitelisted methods and
 * then reload the page. No framework, no build step.
 *
 * Every call is a POST carrying the session's CSRF token. That is not
 * incidental: frappe only validates the token on unsafe HTTP methods, so a
 * state-changing call made any other way would not be checked at all.
 */
(function () {
	"use strict";

	function csrfToken() {
		return (window.frappe && window.frappe.csrf_token) || "";
	}

	function serverMessage(payload) {
		var messages = [];
		try {
			messages = JSON.parse(payload._server_messages || "[]");
		} catch (e) {
			messages = [];
		}
		for (var i = 0; i < messages.length; i++) {
			var entry = messages[i];
			try {
				entry = JSON.parse(entry);
			} catch (e) {
				/* already a plain string */
			}
			var text = (entry && entry.message) || entry;
			if (text) {
				// Server messages may carry markup; render them as text.
				var holder = document.createElement("div");
				holder.innerHTML = text;
				return (holder.textContent || "").trim();
			}
		}
		return (payload.message && payload.message.toString && typeof payload.message === "string")
			? payload.message
			: "";
	}

	function readResponse(response) {
		return response.text().then(function (text) {
			var payload = {};
			try {
				payload = JSON.parse(text);
			} catch (e) {
				payload = {};
			}
			if (response.ok) {
				return payload.message;
			}
			throw new Error(serverMessage(payload) || "Something went wrong. Please try again.");
		});
	}

	function call(method, args) {
		return fetch("/api/method/" + method, {
			method: "POST",
			credentials: "same-origin",
			headers: {
				"Content-Type": "application/json",
				Accept: "application/json",
				"X-Frappe-CSRF-Token": csrfToken()
			},
			body: JSON.stringify(args || {})
		}).then(readResponse);
	}

	/* Uploaded with no doctype/docname on purpose: frappe's upload_file
	 * demands write permission on the target document, and a client role
	 * holds read only. The file goes up unattached and create_portal_ticket
	 * links it to the new ticket server-side, after checking that this
	 * session owns it. */
	function upload(file) {
		var form = new FormData();
		form.append("file", file, file.name);
		form.append("is_private", "1");
		return fetch("/api/method/upload_file", {
			method: "POST",
			credentials: "same-origin",
			headers: { "X-Frappe-CSRF-Token": csrfToken() },
			body: form
		}).then(readResponse);
	}

	function showError(holder, error) {
		if (!holder) {
			return;
		}
		holder.textContent = (error && error.message) || "Something went wrong. Please try again.";
		holder.scrollIntoView({ block: "nearest" });
	}

	function clearError(holder) {
		if (holder) {
			holder.textContent = "";
		}
	}

	/* Keeps a submit button from firing twice while the request is in
	 * flight -- a double-posted ticket is the obvious failure here. */
	function busy(button, label) {
		if (!button) {
			return function () {};
		}
		var original = button.textContent;
		button.disabled = true;
		if (label) {
			button.textContent = label;
		}
		return function () {
			button.disabled = false;
			button.textContent = original;
		};
	}

	window.authcorPortal = {
		call: call,
		upload: upload,
		showError: showError,
		clearError: clearError,
		busy: busy
	};
})();
