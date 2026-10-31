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
| `access` | verified issuer/subject, unique aliases and reader/writer checks |
| `http` | `ServiceConfig`: how any server goes on the network (its own issuer, an external one, access keys; optional Pond heartbeat) |
| `issuer` | an OAuth 2.1 authorization server in one SQLite file: pre-registered and metadata-document clients, optional dynamic registration, the approval page; `boring-service-oauth` |
| `pond` | the parts `ServiceConfig` is built from: token introspection, the `server/discover` heartbeat (`attach_heartbeat`), registration, `wrap_app` |
| `process` | standalone tree containment and wait/cleanup settlement; no server dependency |

## Putting a server on the network

Every server, service or not, starts from one JSON file (`--config`), read by
`ServiceConfig`. Where its tokens come from:

| Section | Tokens come from | For |
|---|---|---|
| `issuer` | the server itself | a lone server used from Claude, ChatGPT, VS Code |
| `oauth` | an external issuer, by introspection (a Crocodile, Keycloak, Entra) | family installs, companies |
| `keys` | nobody: static access keys (`boring-service-keys`); combines with either | scripts, local clients |
| `pond` | (heartbeat registration with a Crocodile, beside `oauth`) | family installs |

`access` lists the subjects the issuer vouches for, each `reader` or `writer`;
empty access denies everyone. Tools read the caller with `calling_author()`,
never from an argument; wrap them with `permitted(fn, "reader"|"writer")`.

How each source is configured and what an operator sees (sign-in, client
registration, the approval page, examples for every mode):
boring-services [CONFIGURATION](https://github.com/the-boring-crocodile/boring-services/blob/main/docs/CONFIGURATION.md).

A family service instead names the Crocodile in `oauth` (its `/introspect`, the
service's `svc_` id and secret from `boring-crocodile-admin service-add`), lists
the Ponds it serves in `access` and names the Crocodile again in `pond`. That is
the one way a service checks its callers: the Pond is the token's subject,
read with `calling_author()`.

## Using it

Services depend on boring-service-kit 0.7 (it brings boring-registry) and pin the exact wheels in their lock file.

```python
from boring_service_kit.http import ServiceConfig, add_config_argument
from boring_service_kit.pond import attach_heartbeat

def make_server(auth, on_discover):
    server = MCPServer("my-service", **auth)   # token check or issuer, and its settings
    attach_heartbeat(server, on_discover)       # no-op without a pond section
    ...  # tools, wrapped with permitted(); calling_author() names the caller
    return server

ServiceConfig(args.config).serve(make_server)
```

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
