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
  caller's own service grants. Authority/store ownership stays in MCP.
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
