import frappe
from frappe import _


def mark_password_login_attempt(login_manager=None, **kwargs):
	"""before_login fires only for password-login submissions -- magic-link
	login goes through LoginManager.login_as() directly and never reaches
	this hook. It also fires before authenticate() verifies the password.

	Mark the request so enforce_password_login_roles (on_login, which fires
	*after* verification but also fires for magic-link logins) can tell
	this one came through the password path, without re-checking the
	password itself.
	"""
	frappe.local.flags.in_password_login = True


def enforce_password_login_roles(login_manager, **kwargs):
	"""Reject password logins for users outside the AC Auth Settings
	allowlist.

	Runs on the on_login hook, which fires only after authenticate() has
	already verified the password (frappe/auth.py: LoginManager.login() ->
	authenticate() -> post_login() -> run_trigger("on_login")). A wrong
	password never reaches here -- authenticate() already raised
	AuthenticationError with its own "Invalid login credentials" message
	before post_login() runs, whether or not the account exists or is
	role-restricted. This hook only ever sees an already-authenticated
	user, so it can give the specific "not enabled" message without
	leaking anything a wrong-password attempt wouldn't already leak.

	on_login also fires for magic-link logins (login_as() -> post_login()
	too), so this only acts when mark_password_login_attempt (before_login)
	set the marker for this request.
	"""
	if not frappe.local.flags.get("in_password_login"):
		return

	user = login_manager.user

	# Break-glass admin: exempt unconditionally, before any database lookup.
	if user == "Administrator":
		return

	try:
		settings = frappe.get_cached_doc("AC Auth Settings")
	except frappe.DoesNotExistError:
		# No settings record yet -- fresh site or a failed/partial
		# migration. An unconfigured gate must fail open: it must not lock
		# out every password login as a side effect of missing setup.
		return

	allowed_roles = {row.role for row in settings.password_login_roles}

	if not allowed_roles:
		# Allowlist cleared or never populated. Same fail-open reasoning as
		# above: an empty allowlist must not mean "nobody can use
		# passwords" -- that locks out everyone. This is a code decision,
		# not something a data migration needs to guarantee.
		return

	if not allowed_roles & set(frappe.get_roles(user)):
		# Mirror LoginManager.fail()'s own pattern: set response["message"]
		# directly rather than relying only on the msgprint/message_log
		# path, so this failure is exposed the same way an invalid-
		# credentials failure already is.
		message = _("Password login is not enabled for your account. Use the emailed sign-in link instead.")
		frappe.local.response["message"] = message
		frappe.throw(message, frappe.AuthenticationError)
