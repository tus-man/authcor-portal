"""PHASE-5-NOTIFICATIONS.md section 6. The "how" for the one channel this
session builds -- Telegram (section 7) and WhatsApp (section 8) each add a
sender here and a register_channel() call, without touching notify() or
any recipient resolver.
"""

import frappe

from authcor.notifications import register_channel


def send_email(recipient, template, context, subject):
	"""Templates are Jinja files under authcor/templates/emails/, one per
	event -- version-controlled and reviewable in a diff, not Email
	Template records that drift between environments
	(PHASE-5-NOTIFICATIONS.md section 6). frappe.sendmail's own
	template=/args= support (frappe.utils.jinja.get_email_from_template)
	already resolves both the .html and the required plain-text .txt
	alternative for us -- no bespoke Jinja environment needed here.

	Raises if the recipient has no email on file, or if sendmail itself
	fails -- both are caught by _deliver's try/except and logged Failed,
	never raised into whatever called notify()."""
	email = frappe.db.get_value("User", recipient, "email")
	if not email:
		raise ValueError(f"{recipient} has no email address on file.")

	frappe.sendmail(
		recipients=[email],
		subject=subject,
		template=template,
		args=context,
		now=False,
	)
	return email


register_channel("Email", send_email)
