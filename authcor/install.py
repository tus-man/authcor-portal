"""Install-time and test-time setup. Both entry points are wired up in
hooks.py.

Everything in after_install has to be safe to run against a site that
already has the app, because authcor/patches/ calls the same code to
backfill sites installed before it existed.
"""

import frappe

from authcor.roles import sync_role_permissions

TEST_EMAIL_ACCOUNT = "Authcor Test Outgoing"


def after_install():
	sync_role_permissions()


def before_tests():
	"""Runs only under `bench run-tests` -- frappe calls this from
	frappe.testing.environment._run_before_test_hooks, and nothing else
	calls it. Deliberately not part of after_install: what it creates has no
	business existing on a production site.
	"""
	_ensure_default_outgoing_email_account()
	frappe.db.commit()


def _ensure_default_outgoing_email_account():
	"""Give the test site something for frappe.sendmail to resolve a sender
	    against.

	    Notifications are delivered inline in tests (authcor/tests/__init__.py),
	    so every ticket insert reaches channels.send_email -> frappe.sendmail,
	    which resolves the outgoing account while *building* the queue row:
	    QueueBuilder.sender -> EmailAccount.find_outgoing(_raise_error=True),
	    with no test-mode shortcut on that path. With no default outgoing
	    account it raises OutgoingEmailError, _deliver catches it and logs the
	    notification Failed instead of Sent -- which is a real failure of
	    test_notifications' Sent assertions and a confusing one, since nothing
	    in the app is broken.

	    Nothing is ever sent. smtp_server points at localhost and no test opens
	    a socket; Email Account.validate returns early under frappe.in_test
	    before it would try to connect or demand a password.

	A no-op on any site that already has a default outgoing account -- a dev
	    site with a real Mailpit account keeps it, and the suite does not quietly
	    take over which account outgoing mail goes through.
	"""
	if not frappe.in_test:
		# Refuse rather than trust the caller. This function creates an
		# enabled, default-outgoing Email Account; on a production site that
		# would silently become the account every outgoing mail is sent
		# through.
		frappe.throw("authcor.install.before_tests may only run in test mode.")

	if frappe.db.exists("Email Account", {"enable_outgoing": 1, "default_outgoing": 1}):
		return

	frappe.get_doc(
		doctype="Email Account",
		email_account_name=TEST_EMAIL_ACCOUNT,
		email_id="test-outgoing@authcor.test",
		enable_incoming=0,
		enable_outgoing=1,
		default_outgoing=1,
		no_smtp_authentication=1,
		smtp_server="localhost",
		smtp_port=25,
	).insert(ignore_permissions=True)
