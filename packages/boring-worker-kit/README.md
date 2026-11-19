# boring-worker-kit

Canonical Linux worker operations, maintained in boring-pond-common.

`file_refs.prepare_call(files, arguments, save_to=None)` admits a service call's file work
without reading files. It checks marker shapes/counts and configured read/write scope through
`FileTools.prepare`; a batch owner calls it before executing any step. Actual file existence,
contents, byte limits and current hashes are checked by `call_service`/`resolve` at execution,
so earlier steps may create or update referenced files and save targets. One traversal owns
reference discovery, admission and resolution. Direct service calls also use this boundary.
MCP and boring-pirates depend on the same wheel. The MCP module names remain
compatibility imports for callers/tests; they contain no second implementation.

- `files`, `file_policy`, `batch_contracts`: named file roots, strict admission,
  formats/CAS/locks/limits. Uses the declared canonical boring-file-core dependency.
- `execution`, `process`, `task_client`, `task_runner`, `host_limits`: command
  admission, output, cancellation and confirmed per-command cgroup cleanup.
  Site adapters own configuration and fixed-UID systemd installation.
- `services`, `service_contract`: stateless MCP HTTP client, isolated bearer
  context and unchanged refusal/failure classification.
- `local_authority`: bounded authenticated Unix peer transport for a local
  caller's own service grants and registration identity. `request("registration",
  pond=NAME)` selects a Pond; `request("token", SERVICE)` selects a service.
  The selectors are mutually exclusive. Authority/store ownership stays in MCP.
- `tools`: provider-neutral schemas generated from maintained admission models.
  Mistral message conversion stays in the Pirate adapter.

`ExecutionSettings` is the structural execution configuration contract; the
MCP site settings and Pirate adapter satisfy it. `EnvironmentSettings` parses
root-controlled JSON for `boring-worker-task --config ... [--section execution]`.
Production Pirate exec requires the fixed task socket; there is no implicit
process-group fallback. The existing explicit local executor remains available
for development fixtures only. The task runner verifies root-controlled config
ancestry and its real systemd cgroup before admitting a command.

Cleanup recovery: TaskCgroup retains a directory descriptor for
the validated original group and owns population observation. TaskClient transfers
that observer on a ContainmentError; EnvironmentExecutor owns admission and retains
all unresolved groups. A new request rechecks them once. Admission resumes only
after every group is empty/removed; unreadable state stays blocked. A failure with
no validated identity requires operator intervention. Commands are never replayed.
Systemd remains the owner of service teardown and escalation.

No OAuth server/agent approvals, service grants database, provider loop or site
installer belongs here. Windows callers continue to use file-core/service-kit;
this Linux worker package is not introduced into pondNT.

Its tests are in tests/ here; consumers test only their own use of it. Install
Common's wheels from one release together, never mixed with older ones.

## Outcomes

The classes come from [boring-outcomes](../boring-outcomes/README.md).
Refused means nothing started or changed; Failed means work started and
may have had effects. as_data() renders flat status, code, message and
detail fields. Invalid codes, messages or non-JSON details are programming
errors. There is no nested details rendering.

Service tool errors without a valid outcome and JSON-RPC errors use failed/upstream_error.
The former retains the service text; the latter retains service_code. Valid service
outcomes keep their own codes and details unchanged.

Worker-kit also exports Refused = Refused for this release only.
FileTools.read_whole(root, path, max_bytes) returns (bytes, sha256).
It uses the existing root/deny/reserved-path and no-follow read policy and the
same bounded worker pool. The caller's positive byte cap replaces the text
tool's max_file_bytes cap. It neither decodes nor returns MCP image content.

An expired deadline before a service dispatch is refused/not_admitted. If a
save fails after the service ran, save_to_refused or save_to_failed embeds
as_data(). A failed save uses timeout when still running at the deadline,
or write_failed for a write error, with effects unknown. The service answer
is retained under the existing inline size limit.
