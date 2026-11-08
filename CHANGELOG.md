# Changelog

Every release is listed here, newest first. Versions follow [Semantic Versioning](https://semver.org/).
Before 1.0, a change to the middle number (0.1 to 0.2) can break your code, and a change to the last
number (0.1.0 to 0.1.1) only fixes things. Each entry says what to change when you upgrade.

## [0.1.2] - 2026-11-08

Documentation and testing only. Nothing to change when you upgrade, unless you use signup on Django
6.1 or later without `MAILERS` set.

- The signup guide says how to set up email. On Django 6.1 and later that is `MAILERS`, which Django
  2028 will require; the guide shows how to support older versions too.
- The example project configures email for each supported Django version.
- The test suite runs in parallel and no longer empties the database twice after each test, so the
  Postgres tests take about a quarter of the time.

## [0.1.1] - 2026-11-07

Documentation and testing only. Nothing to change when you upgrade.

- Postgres 15 or newer is supported, and CI now tests Postgres 15 as well as 18.
- A changelog, linked from the README.

## [0.1.0] - 2026-11-07

The first release.

- Tenant models whose `objects` manager filters by the current tenant, and refuses to run with no
  tenant set.
- Postgres row-level security on every tenant table, on by default, with a check that reports
  tables whose protection is missing or out of date.
- Tenants, memberships and roles, with invitations, join requests, a tenant switcher and logged
  staff access.
- Sharing records with other tenants, read-only or read-write.
- Protection for third-party models, through a link table and no change to their columns.
- An optional signup page that creates a tenant, with email activation for new users.
- Support for Django 5.2, 6.0 and 6.1 on Python 3.11 to 3.15.
