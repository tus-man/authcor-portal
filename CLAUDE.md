# Authcor Portal — working notes for Claude Code

Remote hands & logistics dispatch portal for Authcor (Singapore). Frappe Framework app.
Full specification lives in `docs/PLAN.md` — read it before designing anything new.

---

## Environment

- **Frappe v16.** Pinned. Do not use v15-era APIs; many blog posts and forum answers are v15 and will not match.
- Python 3.14, Node 24.
- Development runs in the `frappe_docker` devcontainer. Bench root is `/workspace/development/frappe-bench`.
- Dev site: `dev.localhost`. Developer mode is on.
- The framework source is at `apps/frappe/`. **Grep it before assuming an API exists** — this is the single most reliable way to avoid inventing method signatures.

## Repo layout

The git repo is `apps/authcor/`. Inside it, the Python package is `apps/authcor/authcor/`.
The doubled name is Frappe convention: outer is the repo, inner is the importable module.

- Tests go in `authcor/tests/` — inside the package, not at the repo root.
- Never commit anything from `sites/` — it contains `site_config.json` with secrets in plaintext.

## Conventions

- **DocType prefix is `AC`.** Every DocType: `AC Smart Hands Request`, `AC Time Entry`, etc. Names are globally unique per site and become table names (`tabAC Time Entry`). Never rename after creation.
- DocTypes are created **in the Desk UI**, not by hand-writing JSON. Developer mode writes the JSON into the app folder; that JSON gets committed.
- Claude Code writes: controllers, `hooks.py`, whitelisted methods, portal templates, client scripts, tests.
- Claude Code does **not** run `bench` commands. The developer runs those and pastes back output.

## Testing

```bash
bench --site dev.localhost run-tests --app authcor
```

Use `IntegrationTestCase` from `frappe.tests` for new tests. `FrappeTestCase` is legacy in v16 and emits a deprecation notice.

CI runs on push to `main` and on PRs: builds a clean bench, installs the app on a fresh site, runs the suite. Roughly 2 minutes.

## Local email

Mail is caught by Mailpit, not delivered.

- SMTP: `mailpit:1025` (container-internal only)
- Inbox: `http://localhost:8025`
- Email Account `Noreply` / `noreply@authcor.local` is the default outgoing account.

Mailpit is defined in `.devcontainer/docker-compose.yml`, which is **outside this repo**. See `docs/DEV-SETUP.md` to recreate it.

## Framework behaviours already discovered

Hard-won; don't rediscover these.

- **`bench console` runs in an uncommitted transaction.** Changes vanish on exit unless you call `frappe.db.commit()`.
- **`frappe.sendmail(..., now=True)`** bypasses the queue and surfaces errors immediately. Best tool for debugging email.
- **`frappe.db.exists("User", email)` checks the record *name*, not the email field.** For normal users the name is their email, but **Administrator's name is `Administrator`** — so Administrator cannot use magic link login. This is structural, not configurable.
- **`send_login_link` fails silently by design.** Every exception path calls `frappe.clear_messages()`; unknown emails are swallowed with no log entry so the form can't be used to enumerate accounts. Silence means a guard clause was hit — read the source rather than adding logging.
- **`rate_limit_email_link_login = 0` means 5 per hour**, not unlimited. The window is per hour, not per minute.
- Login links are single-use: `login_via_key` deletes the cache key on consumption.
- Background job failures appear only in **Error Log** (`/app/error-log`), never in the terminal.
- **`before_login` fires only for password-login submissions.** `LoginManager.login()` (`frappe/auth.py`) calls `run_trigger("before_login")` before `authenticate()`. Magic-link login and impersonation go through `login_as()`, which never calls `login()` — so `before_login` can't fire for them. It's a real, usable hook even though frappe core never registers a handler for it itself.
- **`before_login` runs before `usr` is resolved to a canonical User name.** `authenticate()` resolves the submitted string via `User.find_by_credentials()`, which honors `allow_login_using_mobile_number` / `allow_login_using_user_name`. A `before_login` hook that needs the real user must call `User.find_by_credentials(usr, "", validate_password=False)` itself rather than assuming `frappe.form_dict.get("usr")` is already the User name.
- **A Single DocType's `get_doc`/`get_cached_doc` does not raise `DoesNotExistError` just because no data has ever been saved** — it returns default/empty field values instead. The exception only fires if the DocType record itself is missing from the schema (failed or partial migration). Worth an explicit `try/except` if a hook depends on a Single that might not exist yet.
- **`frappe.get_roles(user)` is safe to call with an explicit `user` argument before a session exists** (e.g. from `before_login`) — it queries `Has Role` directly and only falls back to `frappe.session.user` when the argument is omitted.
- **`on_login` fires after password verification, but also fires for magic-link login** (`login_as()` → `post_login()` → `run_trigger("on_login")`, same as the password path). It is not password-specific the way `before_login` is. To gate password logins specifically *after* the password is verified (so a wrong password never triggers a different error message than usual), set a marker in `before_login` (`frappe.local.flags` — resets every request, safe to use as a per-request signal) and check it in `on_login`. Don't monkeypatch `LoginManager.authenticate` for this; the two-hook marker does it without touching core.
- **`LoginManager.fail()` sets `frappe.local.response["message"]` directly; a bare `frappe.throw(msg, exc)` does not** — it only reaches the client via `_server_messages`/`messages` in the JSON body, a different field. A custom rejection hook that should surface identically to the built-in "Invalid login credentials" failure (e.g. in the login form UI, or in a test asserting on `response.json()["message"]`) must set `frappe.local.response["message"]` explicitly too, not just call `frappe.throw()`.
- **Testing login: construct `LoginManager()` only via a real HTTP request, not directly in test code.** `frappe.auth.HTTPRequest.__init__` sets up `frappe.local.cookie_manager` and `frappe.local.request_ip` before creating the `LoginManager`; a bare `frappe.auth.LoginManager()` call in a test skips that setup and breaks partway through (e.g. on `cookie_manager.init_cookies()`). Drive password-login tests with real `requests.post(...)`/`FrappeClient` calls against the running site instead — this also means test DB writes (`user.insert()`, `settings.save()`) need an explicit `frappe.db.commit()` to be visible to that separate process.
- frappe.get_all bypasses permissions entirely; frappe.get_list enforces them. Any whitelisted method using get_all needs its own explicit has_permission check. This one matters immediately — the Phase 2b pool query is exactly that shape.
- fetch_from on a field pulls values automatically via linkfield.targetfield, no code needed
- eval: in Depends On boxes trips the JS linter; the warning is cosmetic
- Only Administrator and System Manager read Page by default in v16 — every Desk-facing role needs an explicit read grant or users hit "No permission for Page" at login
- DocType JSON: Desk is the source of truth in development. Editing the file directly needs a bench migrate to take effect, and a later Desk edit will overwrite it from the database.
- Whitelisted methods need a client-script button to be reachable from the UI — building the method isn't enough
- Frappe datetime fields hold strings before the database round-trip. frappe.utils.now() returns a string, and self.creation in before_insert is a string, not a datetime. Any function taking a datetime should normalise with get_datetime() at the boundary. Tests using frappe.get_doc() get real datetimes back and won't catch this — write boundary tests that pass ISO strings directly.
- has_permission and permission_query_conditions hooks can only deny — they cannot grant permission that isn't already there. A predicate that doesn't match a caller denies them outright, before User Permissions apply. So gate the restriction on the specific roles being restricted, never on "everyone except X" — anyone not matched is denied rather than falling through.
- Test fixture helpers that insert a record under a fixed, human-chosen name (`_make_city`, `_make_customer`, `_make_holiday_calendar`, a User by fixed email, an AC Engineer Profile by that user, etc.) must be get-or-create, not a blind `.insert()` — same "get-or-mutate" shape as `sla_fixtures.ensure_sla_policy`. A test class that forces `frappe.db.commit()` (claim/add_engineer/submit/mark_reimbursed, or a real login) breaks `IntegrationTestCase`'s normal per-test rollback for everything already pending; if that run is then interrupted before its own `addClassCleanup` teardown runs, the fixture is left permanently on the site. CI never hits this — it builds a fresh site every run — but a dev site reused across many local runs will, the next time that same fixture name is inserted. `AC Smart Hands Request` / `AC Time Entry` / `AC Expense Claim` / `AC Engineer Count Request` tickets are naming-series-autonamed and don't need this — a fresh name is generated every insert regardless of what's already on the site.
- The same leftover-fixture failure isn't only a primary-key/autoname thing. **`User Permission`'s own `validate()` raises `DuplicateEntryError` for a second identical `(user, allow, for_value)` row regardless of its own random hash name** (`frappe/core/doctype/user_permission/user_permission.py: validate_user_permission`) — so `_grant_customer_access`-style helpers need the same get-or-create treatment (`frappe.db.exists("User Permission", {"user":..., "allow":..., "for_value":...})`) even though the doctype's name was never the collision. When adding a new fixture helper, don't reason from "is the name fixed" alone — check whether the target doctype's own `validate()` enforces a uniqueness rule on some other field combination.

## Navigating Desk

`Ctrl+K` is search in v16 (not `Ctrl+G`). URL navigation is more reliable: `/app/<doctype-name>` in lowercase with hyphens, e.g. `/app/system-settings`, `/app/email-queue`.

## Security rules

- Never commit secrets. Config goes in `site_config.json` or environment variables.
- NRIC/FIN uses the `Password` fieldtype (encrypted at rest) plus permlevel restriction. It is PDPA-regulated — see `docs/PLAN.md` §9 before touching anything that reads or transmits it.
- Rate and billable-amount fields sit at permlevel 1 and must never be exposed to client-facing roles.
- Never run `bench migrate` against production.
- Committing and non-committing fixtures cannot be mixed in one test class. Once anything in the class commits — including indirectly, via code under test that commits — every fixture created in setUpClass is committed too, whether intended or not, and survives the per-test rollback. Any class whose code path commits must use the committing fixture helpers throughout and register addClassCleanup for all of them. Symptoms are stale records and failures that appear on a reused dev site but pass in CI

## Build order

Phases are defined in `docs/PLAN.md` §8. Work one phase per session. Commit at phase boundaries.

Currently: **Phase 0 — login model.** Magic link confirmed working natively. Remaining: per-role password toggle, break-glass admin exemption, production rate limit.

Claude Code must not edit DocType JSON. If a schema change is needed, say so and stop. File edits don't reach the running site until bench migrate, so the change looks applied but isn't — and the resulting test failures point at the wrong cause.