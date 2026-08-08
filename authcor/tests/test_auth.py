from types import SimpleNamespace
from unittest.mock import patch

from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import update_password


def _add_user(email, password, roles=None, username=None):
	user = frappe.get_doc(
		doctype="User",
		email=email,
		first_name=email.split("@", 1)[0],
		username=username,
		send_welcome_email=0,
	).insert(ignore_permissions=True)
	user.new_password = password
	user.save(ignore_permissions=True)
	for role in roles or []:
		user.add_roles(role)
	return user


def _seed_request_locals():
	"""LoginManager normally runs inside a real HTTP request:
	frappe.auth.HTTPRequest.__init__ sets request_ip and cookie_manager
	before constructing it, and frappe.local.request is the real werkzeug
	Request (Session.__init__ reads request.cookies when resuming). There's
	no live web server reachable during `bench run-tests`, so seed the same
	thread-local state ourselves instead of driving this over real HTTP."""
	frappe.local.request_ip = "127.0.0.1"
	frappe.local.cookie_manager = frappe.auth.CookieManager()
	frappe.local.response = frappe._dict({"docs": []})
	frappe.local.request = Request(EnvironBuilder(method="POST", path="/").get_environ())


def _attempt_password_login(usr, pwd):
	"""Drive the real password-login path (before_login -> authenticate ->
	on_login) in-process."""
	_seed_request_locals()
	frappe.local.form_dict = frappe._dict({"cmd": "login", "usr": usr, "pwd": pwd})
	return frappe.auth.LoginManager()


def _attempt_magic_link_login(user):
	"""Drive LoginManager.login_as() the way login_via_key does, without
	going through __init__'s password-login dispatch at all -- but still
	initialize the same slots __init__ would, before login_as() (via
	post_login -> make_session) reads them."""
	_seed_request_locals()
	login_manager = frappe.auth.LoginManager.__new__(frappe.auth.LoginManager)
	login_manager.user = None
	login_manager.info = None
	login_manager.full_name = None
	login_manager.user_type = None
	login_manager.login_as(user)
	return login_manager


def _set_allowed_roles(test_case, roles):
	settings = frappe.get_single("AC Auth Settings")
	settings.set("password_login_roles", [{"role": role} for role in roles])
	settings.save(ignore_permissions=True)
	frappe.clear_cache()
	test_case.addCleanup(_reset_allowed_roles, test_case)


def _reset_allowed_roles(test_case):
	settings = frappe.get_single("AC Auth Settings")
	settings.set("password_login_roles", [])
	settings.save(ignore_permissions=True)
	frappe.clear_cache()


def _set_system_setting(key, value):
	frappe.db.set_single_value("System Settings", key, value)
	frappe.clear_cache()


class TestPasswordLoginRoleGate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# This site's site_config.json has no admin_password, so
		# IntegrationTestCase.ADMIN_PASSWORD is None -- set a known
		# password within the test transaction instead (rolled back with
		# everything else at class teardown).
		cls.ADMIN_PASSWORD = "test_admin_pwd_012!"
		update_password("Administrator", cls.ADMIN_PASSWORD)
		cls.password = "test_pwd_012!"
		cls.allowed_user = _add_user("gate_allowed@authcor.test", cls.password, roles=["System Manager"])
		cls.blocked_user = _add_user("gate_blocked@authcor.test", cls.password, roles=["Sales User"])
		cls.username_user = _add_user(
			"gate_username@authcor.test",
			cls.password,
			roles=["System Manager"],
			username="gate_username_login",
		)

	def tearDown(self):
		frappe.local.form_dict = frappe._dict()
		frappe.local.flags.pop("in_password_login", None)
		super().tearDown()

	def test_administrator_password_login_without_settings_record(self):
		"""Administrator must be able to log in by password even before
		AC Auth Settings has ever been configured -- break-glass can't
		depend on a settings record existing."""
		login_manager = _attempt_password_login("Administrator", self.ADMIN_PASSWORD)
		self.assertEqual(login_manager.user, "Administrator")

	def test_password_login_allowed_when_allowlist_empty(self):
		"""An unconfigured/empty allowlist must fail open, not lock every
		password login out, on a fresh site or after the allowlist is
		cleared."""
		_set_allowed_roles(self, [])

		login_manager = _attempt_password_login(self.blocked_user.name, self.password)
		self.assertEqual(login_manager.user, self.blocked_user.name)

	def test_password_login_allowed_inside_allowlist(self):
		_set_allowed_roles(self, ["System Manager"])

		login_manager = _attempt_password_login(self.allowed_user.name, self.password)
		self.assertEqual(login_manager.user, self.allowed_user.name)

	def test_password_login_rejected_outside_allowlist(self):
		"""Correct password, disallowed role: authenticate() must succeed
		first, and only then does the role check reject with the specific,
		helpful message."""
		_set_allowed_roles(self, ["System Manager"])

		with self.assertRaises(frappe.AuthenticationError):
			_attempt_password_login(self.blocked_user.name, self.password)

		self.assertIn("not enabled", frappe.local.response.get("message", ""))

	def test_wrong_password_on_disallowed_account_gives_generic_error(self):
		"""The bug this design specifically fixes: a wrong password on a
		role-restricted account must fail with the exact same generic
		"Invalid login credentials" authenticate() gives for any other
		wrong password -- never the role-specific message, which would
		otherwise leak that the account exists and is disallowed."""
		_set_allowed_roles(self, ["System Manager"])

		with self.assertRaises(frappe.AuthenticationError):
			_attempt_password_login(self.blocked_user.name, "definitely-wrong-password")

		self.assertEqual(frappe.local.response.get("message"), "Invalid login credentials")

	def test_wrong_password_on_unknown_user_gives_generic_error(self):
		with self.assertRaises(frappe.AuthenticationError):
			_attempt_password_login("no_such_user@authcor.test", "whatever")

		self.assertEqual(frappe.local.response.get("message"), "Invalid login credentials")

	def test_login_via_username_still_gated_by_role(self):
		"""authenticate() resolves `usr` (which may be a username, not the
		User name) before this hook ever runs, so the gate naturally reads
		the already-resolved, canonical login_manager.user."""
		_set_system_setting("allow_login_using_user_name", 1)
		self.addCleanup(_set_system_setting, "allow_login_using_user_name", 0)

		_set_allowed_roles(self, ["System Manager"])

		login_manager = _attempt_password_login("gate_username_login", self.password)
		self.assertEqual(login_manager.user, self.username_user.name)

	def test_magic_link_login_not_gated_by_role(self):
		"""on_login also fires for magic-link logins (LoginManager.login_as()
		-> post_login()); the before_login marker must keep those from
		being gated by this password-only check, even for a role-
		restricted account."""
		_set_allowed_roles(self, ["System Manager"])

		login_manager = _attempt_magic_link_login(self.blocked_user.name)
		self.assertEqual(login_manager.user, self.blocked_user.name)

	def test_password_login_allowed_when_settings_record_missing(self):
		"""Simulate a failed/partial migration where AC Auth Settings
		itself can't be loaded: the gate must fail open rather than raise
		or lock everyone out. This is exercised as a direct call, not via
		LoginManager, since there's no way to make the doctype disappear
		from a real running site without corrupting it."""
		from authcor.auth import enforce_password_login_roles

		frappe.local.flags.in_password_login = True

		with patch("frappe.get_cached_doc", side_effect=frappe.DoesNotExistError):
			enforce_password_login_roles(SimpleNamespace(user=self.blocked_user.name))
