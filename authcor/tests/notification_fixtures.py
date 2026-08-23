"""Shared test helpers for the Phase 5 notification system -- not a test
module itself (no Test* classes, filename doesn't match test_*), just
imported by the test files that exercise notify().

frappe.enqueue's default is_async=True tries to push onto a real redis
queue even under frappe.in_test (see frappe/utils/background_jobs.py:
call_directly is only True when `now=True` is passed explicitly), so a
test calling notify() would otherwise see nothing happen -- the job just
sits queued, unrun, for the lifetime of the test process. synchronous_
delivery() patches authcor.notifications.frappe.enqueue to invoke the
target function immediately instead, the same way a worker eventually
would, so AC Notification Log rows exist to assert on by the time notify()
returns.
"""

from contextlib import contextmanager
from unittest.mock import patch

_ENQUEUE_ONLY_KWARGS = (
	"queue",
	"timeout",
	"event",
	"job_name",
	"is_async",
	"now",
	"enqueue_after_commit",
	"on_success",
	"on_failure",
	"at_front",
	"job_id",
	"deduplicate",
	"at_front_when_starved",
)


def _run_immediately(method, **kwargs):
	for key in _ENQUEUE_ONLY_KWARGS:
		kwargs.pop(key, None)
	return method(**kwargs)


@contextmanager
def synchronous_delivery():
	with patch("authcor.notifications.frappe.enqueue", side_effect=_run_immediately):
		yield
