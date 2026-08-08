from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase


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


def _attempt_password_login(usr, pwd):
	"""Drive the real password-login path (LoginManager.login -> authenticate,
	including the before_login hook) without going over HTTP."""
	frappe.local.form_dict = frappe._dict({"cmd": "login", "usr": usr, "pwd": pwd})
	return frappe.auth.LoginManager()


def _set_allowed_roles(test_case, roles):
	settings = frappe.get_single("AC Auth Settings")
	settings.set("password_login_roles", [{"role": role} for role in roles])
	settings.save(ignore_permissions=True)
	frappe.clear_cache()
	test_case.addCleanup(_set_allowed_roles, test_case, [])


class TestPasswordLoginRoleGate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ADMIN_PASSWORD = frappe.get_conf(cls.TEST_SITE).admin_password
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

	def tearDown(self):
		frappe.local.form_dict = frappe._dict()
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

	def test_password_login_allowed_when_settings_record_missing(self):
		"""Simulate a failed/partial migration where AC Auth Settings
		itself can't be loaded: the gate must fail open rather than raise
		or lock everyone out."""
		with patch("frappe.get_cached_doc", side_effect=frappe.DoesNotExistError):
			login_manager = _attempt_password_login(self.blocked_user.name, self.password)
		self.assertEqual(login_manager.user, self.blocked_user.name)

	def test_password_login_rejected_outside_allowlist(self):
		_set_allowed_roles(self, ["System Manager"])

		with self.assertRaises(frappe.AuthenticationError):
			_attempt_password_login(self.blocked_user.name, self.password)

	def test_password_login_allowed_inside_allowlist(self):
		_set_allowed_roles(self, ["System Manager"])

		login_manager = _attempt_password_login(self.allowed_user.name, self.password)
		self.assertEqual(login_manager.user, self.allowed_user.name)

	def test_identity_resolved_via_username_before_role_check(self):
		"""before_login only sees the raw `usr` string; it must resolve
		through the same username/mobile lookup authenticate() uses, not
		assume `usr` is already the User name."""
		frappe.db.set_single_value("System Settings", "allow_login_using_user_name", 1)
		self.addCleanup(frappe.db.set_single_value, "System Settings", "allow_login_using_user_name", 0)
		frappe.clear_cache()

		_set_allowed_roles(self, ["System Manager"])

		login_manager = _attempt_password_login("gate_username_login", self.password)
		self.assertEqual(login_manager.user, self.username_user.name)

	def test_unresolvable_user_falls_through_to_standard_authentication_failure(self):
		"""A `usr` that doesn't resolve to any User must fail with the same
		generic error authenticate() already raises for unknown users --
		this hook must not become a distinguishable failure mode that
		could be used to probe which accounts exist."""
		with self.assertRaises(frappe.AuthenticationError):
			_attempt_password_login("no_such_user@authcor.test", "whatever")

		self.assertEqual(frappe.local.response.get("message"), "Invalid login credentials")
