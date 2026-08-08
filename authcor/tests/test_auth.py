from types import SimpleNamespace
from unittest.mock import patch

import requests

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_site_url
from frappe.www.login import _generate_temporary_login_link


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
	frappe.db.commit()
	return user


def _password_login(host, usr, pwd):
	"""POST a real password-login request, exactly like the login form (and
	like FrappeClient) does. A real HTTP round trip is required here: the
	before_login/authenticate/on_login sequence depends on request-scoped
	state (cookies, request_ip) that only a real request sets up, and the
	request is handled by the running bench process, not this test process."""
	return requests.post(
		host,
		params={"cmd": "login", "usr": usr, "pwd": pwd},
		headers={"Accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
	)


def _set_allowed_roles(test_case, roles):
	settings = frappe.get_single("AC Auth Settings")
	settings.set("password_login_roles", [{"role": role} for role in roles])
	settings.save(ignore_permissions=True)
	frappe.clear_cache()
	frappe.db.commit()
	test_case.addCleanup(_reset_allowed_roles, test_case)


def _reset_allowed_roles(test_case):
	settings = frappe.get_single("AC Auth Settings")
	settings.set("password_login_roles", [])
	settings.save(ignore_permissions=True)
	frappe.clear_cache()
	frappe.db.commit()


def _set_system_setting(key, value):
	frappe.db.set_single_value("System Settings", key, value)
	frappe.clear_cache()
	frappe.db.commit()


class TestPasswordLoginRoleGate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.HOST_NAME = frappe.get_site_config().host_name or get_site_url(frappe.local.site)
		cls.password = "test_pwd_012!"
		cls.allowed_user = _add_user("gate_allowed@authcor.test", cls.password, roles=["System Manager"])
		cls.blocked_user = _add_user("gate_blocked@authcor.test", cls.password, roles=["Sales User"])
		cls.username_user = _add_user(
			"gate_username@authcor.test",
			cls.password,
			roles=["System Manager"],
			username="gate_username_login",
		)

	@classmethod
	def tearDownClass(cls):
		for email in (
			"gate_allowed@authcor.test",
			"gate_blocked@authcor.test",
			"gate_username@authcor.test",
		):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_administrator_password_login_without_settings_record(self):
		"""Administrator must be able to log in by password even before
		AC Auth Settings has ever been configured -- break-glass can't
		depend on a settings record existing."""
		res = _password_login(self.HOST_NAME, "Administrator", self.ADMIN_PASSWORD)
		self.assertEqual(res.status_code, 200)
		self.assertEqual(res.json().get("message"), "Logged In")

	def test_password_login_allowed_when_allowlist_empty(self):
		"""An unconfigured/empty allowlist must fail open, not lock every
		password login out, on a fresh site or after the allowlist is
		cleared."""
		_set_allowed_roles(self, [])

		res = _password_login(self.HOST_NAME, self.blocked_user.name, self.password)
		self.assertEqual(res.status_code, 200)
		self.assertEqual(res.json().get("message"), "Logged In")

	def test_password_login_allowed_inside_allowlist(self):
		_set_allowed_roles(self, ["System Manager"])

		res = _password_login(self.HOST_NAME, self.allowed_user.name, self.password)
		self.assertEqual(res.status_code, 200)
		self.assertEqual(res.json().get("message"), "Logged In")

	def test_password_login_rejected_outside_allowlist(self):
		"""Correct password, disallowed role: authenticate() must succeed
		first, and only then does the role check reject with the specific,
		helpful message."""
		_set_allowed_roles(self, ["System Manager"])

		res = _password_login(self.HOST_NAME, self.blocked_user.name, self.password)
		self.assertEqual(res.status_code, 401)
		self.assertIn("not enabled", res.json().get("message", ""))

	def test_wrong_password_on_disallowed_account_gives_generic_error(self):
		"""The bug this design specifically fixes: a wrong password on a
		role-restricted account must fail with the exact same generic
		"Invalid login credentials" authenticate() gives for any other
		wrong password -- never the role-specific message, which would
		otherwise leak that the account exists and is disallowed."""
		_set_allowed_roles(self, ["System Manager"])

		res = _password_login(self.HOST_NAME, self.blocked_user.name, "definitely-wrong-password")
		self.assertEqual(res.status_code, 401)
		self.assertEqual(res.json().get("message"), "Invalid login credentials")

	def test_wrong_password_on_unknown_user_gives_generic_error(self):
		res = _password_login(self.HOST_NAME, "no_such_user@authcor.test", "whatever")
		self.assertEqual(res.status_code, 401)
		self.assertEqual(res.json().get("message"), "Invalid login credentials")

	def test_login_via_username_still_gated_by_role(self):
		"""authenticate() resolves `usr` (which may be a username, not the
		User name) before this hook ever runs, so the gate naturally reads
		the already-resolved, canonical login_manager.user."""
		_set_system_setting("allow_login_using_user_name", 1)
		self.addCleanup(_set_system_setting, "allow_login_using_user_name", 0)

		_set_allowed_roles(self, ["System Manager"])

		res = _password_login(self.HOST_NAME, "gate_username_login", self.password)
		self.assertEqual(res.status_code, 200)
		self.assertEqual(res.json().get("message"), "Logged In")

	def test_magic_link_login_not_gated_by_role(self):
		"""on_login also fires for magic-link logins (LoginManager.login_as()
		-> post_login()); the before_login marker must keep those from
		being gated by this password-only check, even for a role-
		restricted account."""
		_set_allowed_roles(self, ["System Manager"])

		link = _generate_temporary_login_link(self.blocked_user.name, 10)
		res = requests.get(link)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(res.cookies.get("sid"))
		self.assertNotEqual(res.cookies.get("sid"), "Guest")

	def test_password_login_allowed_when_settings_record_missing(self):
		"""Simulate a failed/partial migration where AC Auth Settings
		itself can't be loaded: the gate must fail open rather than raise
		or lock everyone out. This is exercised as a direct call, not over
		HTTP, since there's no way to make the doctype disappear from a
		real running site without corrupting it."""
		from authcor.auth import enforce_password_login_roles

		frappe.local.flags.in_password_login = True
		self.addCleanup(frappe.local.flags.pop, "in_password_login", None)

		with patch("frappe.get_cached_doc", side_effect=frappe.DoesNotExistError):
			enforce_password_login_roles(SimpleNamespace(user=self.blocked_user.name))
