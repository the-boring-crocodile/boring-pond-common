# Changelog

## 0.8.0 — 2026-11-19

### Upgrading

Common requires Python 3.12 or newer. Use the matching release and lock files
in each family consumer.

Approval secrets must contain at least 24 characters after trimming
whitespace. A service with its own sign-in now checks this at startup and
again when approving a sign-in. Check the file's length without displaying
its value. Generate a replacement in a private working directory if needed.

~~~sh
sudo python3 -c 'from pathlib import Path; import sys; print(len(Path(sys.argv[1]).read_text(encoding="utf-8").strip()))' /path/to/approval-secret
(umask 077; set -C; openssl rand -hex 32 > new-approval-secret)
~~~

Install the replacement as the component's approval file with its required
ownership and permissions. Repeat the length check on the installed file.

### Changes

- The shared outcome vocabulary gives Linux and Windows tools the same
  refusal and failure shape.
- Boring Deploy checks installation inputs and root-owned paths before
  making changes. Container installs track the managed image in
  boring-image.env and preserve the operator's .env.
- Service authentication uses one explicit mode. Crocodile supplies the
  caller role, access keys carry their own role, and a standalone issuer
  uses the configured role. Container listener settings come from Compose.
- Use job_wait with wait_seconds set to 0 in place of job_status.

## 0.7.13 — 2026-11-07

- service-kit: a config with both `issuer` and `pond` is refused: a Pond calls only with the
  Crocodile's tokens, which a server's own sign-in would refuse. MCP Ferry checked this alone
  until now.
- deploy: every numbered step line (deploy, first install, uninstall) is flushed as it is printed,
  so through a pipe it appears in order with the hooks' output and before a `FAILED` line.

## 0.7.12 — 2026-11-05

- service-kit: shared HTTP startup waits up to 20 seconds when the listener is
  still held during a restart. It logs the wait, reserves the actual socket
  and hands that socket to uvicorn; other bind errors fail immediately.

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
