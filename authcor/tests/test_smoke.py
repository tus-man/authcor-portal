import frappe
from frappe.tests.utils import FrappeTestCase


class TestSmoke(FrappeTestCase):
	def test_app_is_installed(self):
		"""authcor should be installed on the test site."""
		self.assertIn("authcor", frappe.get_installed_apps())