# Changelog

All packages share one version (VERSION, `scripts/version.py`). While the
major version is 0, a breaking API change raises the minor version; consumers
depend on `>=0.N.0,<0.(N+1)`.

## 0.7.11 — 2026-10-30

Everything since 0.7.6. Every family member builds on this release.

- worker-kit: `file_refs` (moved here from the Crocodile, shared with the Pirates). A
  `{"$file": {"root", "path", "sha256"?}}` argument value becomes that file's exact text (at most
  16 references and 512 KiB per call), read through the file tools; `save_to` writes a service's
  answer to a file and returns `saved_to`. After the service ran, a refused save comes back as
  `save_to_refused` (nothing written), a failed or timed-out one as `save_to_failed` with
  `effects: unknown`; the answer is kept. `prepare_call` checks the shape, counts and configured
  read/write authority of references before anything runs, so a batch can refuse a bad later step
  before earlier effects.
- worker-kit: `deadlines`. A request carries its time budget; `budget()` caps it by the
  receiver's limit and refuses before anything is sent when no time is left. `Services.call` and
  `describe` take a `timeout` within that budget.
- worker-kit: `settings.validate_execution(settings)` holds the execution rules once (lowercase
  name, normalized absolute paths, `default_cwd` inside an allowed root, unit prefix
  `<product>-task-<name>`, a direct `system.slice` child slice, output limit within the frame).
  `EnvironmentSettings.from_mapping` uses it; a consumer with its own configuration format calls
  it on what it parsed. Its tests now cover execution, framing and host limits.
- service-kit: own-issuer HTTP apps accept public-client token revocation without a
  `client_secret` form field; authentication, token ownership, CORS and request-size limits keep
  their owners. `configure_issuer_routes` is the shared hook for custom issuer apps.
- service-kit: Windows file locks no longer write an initialization byte before taking the lock;
  two first-time writers could fail with PermissionError.
- service-kit: the unauthenticated heartbeat is documented as a
  [known limit](packages/boring-service-kit/README.md#known-limit-the-heartbeat-is-not-authenticated).
- deploy: Docker stacks of several services. `units(ctx)` names the compose services a deploy
  owns; start, wait and the stopped-stack decision apply to those alone (no `units`: the whole
  stack, as before).
- deploy: `ctx.changed` lets a step that changes the host before the switch report it, so a
  failure says "the host changed before the switch" instead of "nothing changed".
- deploy: `install.seal_check` and `install.seal_folders` are public: the one implementation of
  handing a departing account's folders to root before `userdel`.

## 0.7.6 — 2026-10-21

First public release of the shared libraries of the Pond family: file-core, registry, service-kit, worker-kit and deploy.
