# boring-service-kit

What every boring MCP service shares: one owner for the service side of the Pond's service contract,
boring-crocodile/docs/SERVICES_AND_PIRATES.md, and for the tool conventions.
It lives in boring-pond-common beside boring-registry. Controller protocol
changes and shared implementation changes require coordinated consumer checks.

| Module | What |
|---|---|
| `outcomes` | `Refused` (nothing changed) and `Failed` (may have had effects) |
| `calllog` | one JSON line per tool call, arguments summarised |
| `tools` | compact results, outcomes as `isError`, lean schemas, strict arguments with one-line errors, `default_state_dir(service)` |
| `access` | canonical verified issuer/subject attribution and reader/writer checks |
| `http` | `ServiceConfig`: how any server goes on the network (one explicit Crocodile, keys or own-issuer mode) |
| `issuer` | an OAuth 2.1 authorization server in one SQLite file: pre-registered and metadata-document clients, optional dynamic registration, the approval page; `boring-service-oauth` |
| `pond` | the parts `ServiceConfig` is built from: token introspection, the `server/discover` heartbeat (`attach_heartbeat`), registration, `wrap_app` |
| `process` | standalone tree containment and wait/cleanup settlement; no server dependency |

## Putting a server on the network

Every server, service or not, starts from one JSON file (`--config`), read by
`ServiceConfig`. Where its tokens come from:

Config selects exactly one `mode` and its matching block:

| Mode | Role owner | Configuration |
|---|---|---|
| `crocodile` | the Crocodile's caller grant | `crocodile: {url, client_id, secret_file}` |
| `keys` | the key record | `keys: {file}` |
| `issuer` | one role for approved clients | `issuer: {subject, role, database, approval_secret_file, ...}` |

The same Crocodile authority object supplies introspection and heartbeat
registration; there is no second credential triple or local subject list.
The authenticated introspection answer supplies the caller's role. Positive
answers are cached for at most60seconds (or token expiry, if sooner), so a role
change takes effect within that bound without restarting the service.
Missing or invalid roles refuse authentication. Keys remain live-file checks.
An own issuer accepts only tokens it minted; modes cannot be combined.

`access`, `oauth` and `pond` are refused with their replacement named.
Tools read `calling_author()`, never an identity argument; use
`permitted(fn, "reader"|"writer")`. Attribution remains the canonical JSON
pair of issuer and subject, without configurable aliases.

The kit's listening socket is plain HTTP. Service images declare
`BORING_SERVICE_CONTAINER=1`; Compose owns their listener addresses in
`BORING_SERVICE_LISTENERS`, a JSON object keyed by absolute config path:

    {"/run/config/config.json":{"bind":"0.0.0.0","port":18512}}

Inspection and startup read that same deployment environment. Missing addresses
are refused, never inferred from the public URL. Container JSON refuses
`bind`, `port` and `behind_proxy` with the Compose replacement named.
The deploy checks actual publications: loopback, or no publications and an
explicitly named attached proxy network. The marker declares the image's
execution model; it does not detect or configure Docker.

Native runs keep JSON listener settings and the loopback guard. A native
non-loopback bind requires explicit `behind_proxy=true` and access restricted
to that trusted proxy; the acknowledgement adds no TLS or firewall. Native
runs refuse container listener settings without the image marker.

How each source is configured and what an operator sees (sign-in, client
registration, the approval page, examples for every mode):
boring-services [CONFIGURATION](https://github.com/the-boring-crocodile/boring-services/blob/main/docs/CONFIGURATION.md).

For a family service, the installer obtains the one Crocodile authority
credential from its registration. The public `url` still names the service
itself; `crocodile.url` names the trusted controller.

## Using it

Services depend on boring-service-kit (it brings boring-registry) and pin the exact wheels in their lock file.

```python
from boring_service_kit.http import ServiceConfig, add_config_argument
from boring_service_kit.pond import attach_heartbeat

def make_server(auth, on_discover):
    server = MCPServer("my-service", **auth)   # token check or issuer, and its settings
    attach_heartbeat(server, on_discover)       # no-op outside Crocodile mode
    ...  # tools, wrapped with permitted(); calling_author() names the caller
    return server

ServiceConfig(args.config).serve(make_server)
```

`ServiceConfig.validate(path)` runs the same configuration parser and SDK
settings checks without opening or initializing an issuer database or
starting a server. It returns `None` on success and raises the same input
errors as startup. Referenced client secrets are read; database contents,
write permissions and network reachability remain separate checks. Use it
for a candidate's config check before switching the running service.

`ServiceConfig.inspect(path)` performs the same read-only validation and
returns immutable `ServiceInfo` (URL, bind, port and optional OAuth client
ID). A running config's `info()` returns the same view. It contains no
credentials or runnable service methods; composed servers can share their
frontend checks between inspection and startup.

Tests: `pytest packages/boring-service-kit/tests` with mcp 2.2.0 and
boring-registry installed. `boring_service_kit.testing` has a fake control
plane and an in-process client for services' own tests.

## Known limit: the heartbeat is not authenticated

`attach_heartbeat` answers `server/discover` without a token, and a discover
naming a Pond renews that Pond's liveness. Anyone who reaches the front end
(behind a public proxy route: anyone) can keep a source "alive" after its
worker lost it, so the service does not re-register until the spoof stops.
Availability only: the answer carries the server's name, version and
capabilities, no data; tools still need a token. A ping token from the
registration is planned. Until then, a service
that does not re-register after a worker restart: restart the service.

## Optional process containment

Consumers using Tree install boring-service-kit[process]; its Windows extra
declares pywin32 explicitly. The other service modules do not import it.
Windows starts suspended, assigns a kill-on-close Job Object, then resumes.
Tree.close observes zero active processes before releasing the handle and raises
when cleanup cannot be confirmed. Callers retain the owner on failure.
The POSIX process-group branch is a portable test adapter, not production
Linux containment (which remains the worker/systemd responsibility).

settle is the shared watcher algorithm: wait/stop/timeout, then tree cleanup,
then a terminal result. Callers own admission, state publication and metadata.
No MCP, core server, PowerShell or D365 imports live in this module.
Process stdin/stdout pipe endpoints remain the caller's responsibility after
tree shutdown. The suspended-start and cleanup algorithm came from pondNT.

## Key-file publication

Key administration stages a complete JSON file, flushes it, then renames it into
place. Live verifiers read it on every call; an interrupted in-place overwrite
would invalidate authentication for every caller. This is a separate contract
from ordinary user file edits.

On Windows, existing and staged owner/group/DACL are compared using Windows SDDL,
not raw security-descriptor bytes. ACE order, access masks, inheritance and
protection flags must match; differing offsets/padding and defaulted provenance
bits do not govern this decision. This is conservative structural comparison,
not a general effective-access solver. Custom security differing from the staging
file's inherited security is still refused before publication. The attribute
guard still ignores only archive/normal changes. No ACL repair after rename.

The guard remains limited to owner/group/DACL and the existing attribute mask;
it does not establish preservation of SACLs, named streams or other NTFS metadata.
Use private managed key-file directories, not arbitrary user documents.

Tests: the portable key, security, concurrency and output checks run everywhere;
two more run only on Windows (equivalent security descriptors with relocated
fields, and a key update under inherited security).

## Outcomes

The classes come from [boring-outcomes](../boring-outcomes/README.md).
Refused means nothing started or changed; Failed means work started and
may have had effects. as_data() renders flat status, code, message and
detail fields. Invalid codes, messages or non-JSON details are programming
errors. There is no nested details rendering.

MCP outcomes remain a single compact JSON text block with isError=true,
without structuredContent or a generated output schema.
