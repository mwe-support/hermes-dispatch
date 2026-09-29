# Dashboard profile logs

Removable compatibility for Hermes 0.20.5's Dashboard: `/api/logs` ignores
`profile`, its client omits the selected profile, and a named profile therefore
shows the Dashboard process's log. No Hermes source files are changed.

This Dashboard plugin replaces only `/logs` through the native `tab.override`
extension. It keeps file, level, component, line-count, manual refresh and
five-second auto-refresh controls. Version 1.0.1 skips timer ticks while a
same-scope request is in flight, so responses slower than five seconds can
finish. Same-scope refresh keeps the last completed result visible; filter
changes clear it, and filter/profile changes and unmount still cancel old requests. It uses the host React/i18n/auth SDK and the
native profile-keyed page remount. Requests read the synchronized `?profile=`
after the parent effect; old requests are aborted and cannot populate a newly
selected profile. No frontend build or extra dependency is needed.

Its authenticated plugin API uses Hermes' existing `_config_profile_scope`
(context-local, safe across awaits) and native `get_logs` reader. Validation,
filters and missing-file behavior stay native; invalid/missing profiles do not
fall back to default. The original `/api/logs` remains an upstream endpoint;
external clients needing scoped logs must use the plugin endpoint explicitly.

## Enable and verify

Install with `scripts/install-plugins.sh <dashboard-hermes-home>
dashboard-profile-logs`, then add `dashboard-profile-logs` to that Dashboard
home's `plugins.enabled` list (remove it from `plugins.disabled` if present).
Restart only the Dashboard process and refresh the browser. Gateways and their
profile configuration do not need a restart. Do not put this fix in a cloned
Hermes source tree or copy it into each named profile.

Run `test_profile_logs.py` with the installed Hermes Python and Hermes source
on `PYTHONPATH`, then `node test_frontend.cjs`. The Python test builds temporary
logs for default/product/procurement and tests the native ASGI auth boundary,
profile selection, filters, invalid profiles and concurrent isolation. The
frontend check verifies explicit scope, controls, discarded late responses,
and one-/six-/ten-second polling with both abort-aware and abort-ignoring transports.
Filter fixtures use native log syntax with INFO after ERROR; separate and
combined level/component assertions detect omitted filters.
In the real Dashboard, switch all three profiles and compare responses against
each profile's own log; test rapid switching and manual/automatic refresh.

## Rollback and limits

Remove this plugin from `plugins.enabled`, restart the Dashboard and refresh
the browser. The native log page returns, including its upstream default-only
limitation. Restore the installer backup if replacing an earlier plugin version.
No business logs, credentials, model settings or Gateway services are modified.
The compatibility depends on the installed Dashboard's native plugin SDK,
`ProfileKeyedRoutes`, `_config_profile_scope`, and `get_logs` signature. Remove
it when upstream supplies equivalent profile-scoped logging.
