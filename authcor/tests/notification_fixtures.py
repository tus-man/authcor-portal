"""Shared test helpers for background-job delivery -- not a test module
itself (no Test* classes, filename doesn't match test_*), just imported by
authcor.tests (which installs the suite-wide patch) and by the test files
that exercise notify() directly.

frappe.enqueue's default is_async=True pushes onto a real redis queue even
under frappe.in_test (see frappe/utils/background_jobs.py: call_directly is
only True when `now=True` is passed explicitly, or when is_async=False
outside tests). Nothing in a test process ever runs those jobs, so they
just accumulate: every ticket, shipment and count-request insert in the
suite dispatches through notify(), and the queue filled to the point of
QueueOverloaded. Worse, a test calling notify() saw nothing happen at all
-- the AC Notification Log rows it wanted to assert on were only ever
written by a worker that wasn't there.

run_background_jobs_inline() replaces frappe.enqueue process-wide so jobs
run in-process instead of being queued. authcor.tests calls it at import,
which is before any test in the suite runs; see that module for why that
is the install point.
"""

from contextlib import contextmanager

import frappe
from frappe.utils import background_jobs

_real_enqueue = None


def _inline_enqueue(method, **kwargs):
	"""Stand-in for frappe.enqueue that runs the job here and now.

	Delegates to the real enqueue with now=True rather than calling
	`method` itself, so frappe's own inline path does the work: it
	resolves a dotted-path method string, and frappe.call drops the
	kwargs the target doesn't declare (frappe.get_newargs). Calling
	`method(**kwargs)` directly instead would mean maintaining a list
	here of every enqueue-only kwarg to strip -- and that list cannot be
	complete, because frappe.enqueue takes arbitrary extras through
	**kwargs and some callers pass job-runner metadata that way
	(`description=`, in frappe's own email_account.py).
	"""
	kwargs.pop("is_async", None)
	kwargs.pop("enqueue_after_commit", None)
	kwargs["now"] = True

	# A real worker runs the job in a transaction of its own, which is why
	# authcor.notifications._write_log ends with frappe.db.commit() -- a
	# job gets no automatic end-of-request commit, so it has to flush its
	# own audit row. Run inline, that commit lands in the middle of the
	# calling test's transaction and ends it: every fixture created so far
	# becomes permanent, and IntegrationTestCase's rollback has nothing
	# left to undo (apps/authcor/CLAUDE.md, on mixing committing and
	# non-committing fixtures in one class). Suppressed rather than
	# tolerated, so that making delivery synchronous changes what the
	# suite observes without changing what it commits.
	#
	# _disable_transaction_control is frappe's own "a commit here would be
	# wrong" mechanism, not a monkeypatch of the connection: core raises
	# the same counter around doc-event handlers for the same reason
	# (frappe/model/document.py, in the compose() runner). Being a counter
	# rather than a flag, it nests correctly if an inline job enqueues
	# another.
	frappe.db._disable_transaction_control += 1
	try:
		return _real_enqueue(method, **kwargs)
	finally:
		frappe.db._disable_transaction_control -= 1


def run_background_jobs_inline():
	"""Install _inline_enqueue process-wide. Idempotent.

	Patches both the name the app calls (`frappe.enqueue`) and the
	function in its defining module, which is what makes frappe.enqueue_
	doc and frappe's own function-local `from frappe.utils.background_jobs
	import enqueue` imports go inline too. The one shape it cannot reach
	is a module that bound the name at its own import time, before this
	ran -- several frappe core modules do (twofactor, workflow_action,
	prepared_report, data_import, email_account, backups,
	scheduled_job_type). None of them are on any path this suite
	exercises, and frappe's outgoing mail already passes now=frappe.in_
	test at its one enqueue site (email_queue.py), so nothing reaches
	redis in practice. If a QueueOverloaded ever comes back, that list is
	the place to look first.
	"""
	global _real_enqueue
	if _real_enqueue is not None:
		return
	_real_enqueue = background_jobs.enqueue
	frappe.enqueue = _inline_enqueue
	background_jobs.enqueue = _inline_enqueue


def jobs_run_inline():
	"""Whether the suite-wide patch is installed. For the guard test --
	a silently-missing patch means the queue starts filling again."""
	return frappe.enqueue is _inline_enqueue and background_jobs.enqueue is _inline_enqueue


@contextmanager
def synchronous_delivery():
	"""Kept for the notification tests that document *at the call site*
	that they depend on delivery having happened by the time notify()
	returns. Since the suite-wide install, this is a no-op: it asserts
	that fact rather than arranging it, so a test reading `with
	synchronous_delivery():` is not misled into thinking the behaviour is
	local to that block.
	"""
	run_background_jobs_inline()
	yield
