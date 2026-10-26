"""Suite-wide setup for the authcor tests.

Python imports a package before any module inside it, so importing any
authcor.tests.* module runs this file first -- earlier than any
setUpClass, and whatever the developer asked for (the whole suite, one
module, one test). That is why the background-job patch is installed from
here rather than from a `before_tests` hook: a hook runs only under the
frappe test runner, only for the app being tested, and not at all under
--skip-before-tests, whereas this cannot be bypassed without also failing
to import the tests.

The patch makes frappe.enqueue run jobs in-process. Without it, every
ticket/shipment/count-request insert in the suite leaves a real redis job
behind that no worker in a test process ever runs, and the queue climbs
until frappe refuses to accept more (QueueOverloaded). See
authcor.tests.notification_fixtures for the mechanics.
"""

from authcor.tests.notification_fixtures import run_background_jobs_inline

run_background_jobs_inline()
