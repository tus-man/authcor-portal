import frappe
from frappe import _
from frappe.core.doctype.user.user import User


def enforce_password_login_roles(login_manager=None, **kwargs):
	"""Reject password logins for users outside the AC Auth Settings allowlist.

	Runs on the `before_login` hook, which frappe.auth.LoginManager.login()
	fires only for password-login submissions -- magic-link login goes
	through LoginManager.login_as() directly and never reaches this hook.
	See docs/PLAN.md Phase 0.
	"""
	raw_user = frappe.form_dict.get("usr")

	# Break-glass admin: exempt unconditionally, before any database lookup.
	# The break-glass account must not depend on a query -- or the settings
	# record -- succeeding.
	if raw_user == "Administrator":
		return

	if not raw_user:
		return

	# before_login fires before authenticate() resolves `usr` into a User
	# name -- the raw value may be a username or mobile number instead,
	# depending on System Settings. Reuse the exact resolution
	# authenticate() itself uses (User.find_by_credentials), with
	# validate_password=False so no password check happens here.
	resolved = User.find_by_credentials(raw_user, "", validate_password=False)

	if not resolved:
		# Identity didn't resolve to any User. Explicitly fall through to
		# authenticate(), which will fail on its own with the standard
		# "Invalid login credentials" error -- mirrors send_login_link's
		# silent handling of unknown emails, so this hook can't be used to
		# probe which accounts exist by varying `usr`.
		return

	user_name = resolved["name"]

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

	if not allowed_roles & set(frappe.get_roles(user_name)):
		frappe.throw(
			_("Password login is not enabled for your account. Use the emailed sign-in link instead."),
			frappe.AuthenticationError,
		)
