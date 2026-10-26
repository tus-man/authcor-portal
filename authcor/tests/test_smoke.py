import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.utils import FrappeTestCase

from authcor.tests.notification_fixtures import jobs_run_inline


class TestSmoke(FrappeTestCase):
	def test_app_is_installed(self):
		"""authcor should be installed on the test site."""
		self.assertIn("authcor", frappe.get_installed_apps())


def _probe(seen):
	"""Records what the job could observe about its own transaction."""
	seen["ran"] = True
	seen["transaction_control_disabled"] = frappe.db._disable_transaction_control > 0


class TestBackgroundJobsRunInline(IntegrationTestCase):
	"""Guards authcor.tests' suite-wide frappe.enqueue patch.

	Nothing in a test process runs a queued job, so a suite that queues
	for real fails in a way no assertion catches: the jobs pile up on
	redis run after run until frappe starts refusing them
	(QueueOverloaded), and the failure then lands on whichever test
	happened to be creating a ticket at the time. These tests are the only
	thing that notices the patch going missing -- a renamed frappe.enqueue
	in a future frappe version, or an import that re-binds it.
	"""

	def test_enqueue_is_patched_suite_wide(self):
		self.assertTrue(
			jobs_run_inline(),
			"frappe.enqueue is not the inline stand-in -- the suite is queueing real jobs.",
		)

	def test_enqueue_runs_the_job_in_this_process(self):
		seen = {}
		frappe.enqueue(_probe, queue="short", seen=seen)
		# Not "eventually" -- by the time enqueue returns. An enqueue-only
		# kwarg (queue) is also proof that the real enqueue signature
		# absorbed it rather than it reaching _probe as an argument.
		self.assertTrue(seen.get("ran"))

	def test_an_inline_job_cannot_end_the_calling_transaction(self):
		"""authcor.notifications._write_log commits, which is right in a
		worker and wrong here: it would end the test's transaction and
		make every fixture created so far permanent. The stand-in raises
		frappe's own _disable_transaction_control for the duration, so
		that commit is a no-op; this asserts the job really does run under
		it, and that the counter is put back afterwards."""
		before = frappe.db._disable_transaction_control
		seen = {}
		frappe.enqueue(_probe, seen=seen)
		self.assertTrue(seen.get("transaction_control_disabled"))
		self.assertEqual(frappe.db._disable_transaction_control, before)
