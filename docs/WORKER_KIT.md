# Linux worker operations

boring-worker-kit is the shared worker library used by Crocodile and Pirates.
It supplies file operations, command execution and calls to services under the
account the host application assigns. The host owns its configuration, grants
and systemd installation; installing this wheel alone does not create a Pond
or an agent.

Windows pondNT uses file-core and service-kit instead.
Its [D365 service](https://github.com/the-boring-crocodile/boring-pondnt/blob/main/docs/D365_SERVICE.md)
uses Windows Job Objects. It does not use this Linux worker package.

## What owns each part

| Work | Owner in worker-kit | Host application supplies |
|---|---|---|
| File reads and edits | `files`, `file_policy`, `batch_contracts`, using file-core | Named roots, access and limits |
| Commands and their descendants | `execution`, `task_client`, `task_runner`, `host_limits` | Execution identity, fixed task socket and systemd units |
| MCP calls to services | `services`, `service_contract` | Endpoint and the caller's granted authority |
| Files passed into or saved from a service call | `file_refs` | The same file policy used by ordinary file tools |
| Local service-authority requests | `local_authority` | The Crocodile-owned authority/store |
| Provider-neutral tool schemas | `tools` | Provider-specific message conversion in the agent adapter |

The [package source and implementation notes](../packages/boring-worker-kit/README.md)
name the internal contracts. OAuth approval pages, the service-grants database,
the model-provider loop and site installation belong to the applications.

## Commands: admission, execution, cleanup

A production command-enabled worker connects to a fixed task socket installed
for its execution identity. The task runner verifies root-controlled
configuration and its actual systemd cgroup before admitting the command.
The production entry points refuse a missing task socket. The explicit local
executor exists for development fixtures.

The command slot stays occupied until its owner finishes cleanup. Parent-process
exit is not enough: descendants must be accounted for before a normal terminal
result is returned. Systemd owns service teardown and escalation.

| Observation | Meaning |
|---|---|
| Foreground admission timeout, command not started | This request was not admitted; it started no command |
| Admitted command times out or is cancelled | Its owner stops the task and waits for cleanup; inspect the reported outcome |
| Cleanup confirmed | The process tree settled; prior file, database or external-service effects are not undone |
| Cleanup unconfirmed | The executor blocks new command admission; neither a clean stop nor absence of effects is established |

After unconfirmed cleanup, the executor retains the validated original cgroup
identity. Each new request checks retained groups once. It admits new work
only after all are confirmed empty or removed; unreadable or malformed state
keeps admission blocked. A reused pathname cannot stand in for the original
group. Failure without a validated identity requires operator recovery.
The original command is never replayed.

`ExecutionSettings` describes the host's execution configuration.
`EnvironmentSettings` parses the root-controlled JSON read by
`boring-worker-task --config ... [--section execution]`. Neither contract
grants an application another account's execution authority.

## Service calls with files

A service argument can refer to a file through a `$file` marker, and
`save_to` can name a file for the answer. The same file roots, permissions
and limits apply as for ordinary file tools.

There are two checks:

1. `file_refs.prepare_call(files, arguments, save_to=None)` checks marker
   structure, reference count and configured read/write scope without reading
   file contents. A batch owner runs this before executing any step.
2. At dispatch, `call_service`/`resolve` checks file existence, contents,
   byte limits and current hashes. An earlier admitted batch step can therefore
   create an input or update a save target.

Admission is not a promise that the later read or save will succeed. If saving
fails after the service ran, inspect `save_to_refused` or `save_to_failed`
and the current target before retrying. The service may already have acted.
Direct calls use the same admission boundary as batches.

## Development and verification

Use the matching Common wheels together; each consumer's lock pins their
hashes. From the Common repository, the maintained check builds the wheels,
installs them in a fresh environment and runs every package's tests:

```bash
python scripts/check.py
```

Worker-kit owns [tests in this repository](../packages/boring-worker-kit/tests).
From a development environment already prepared with the matching Common
packages, its focused command is:

```bash
python -m pytest -q packages/boring-worker-kit/tests
```

Crocodile and Pirates add their own integration/type checks. Synthetic cgroup
and process tests establish library contracts; actual systemd startup, shutdown
and recovery are separate native application acceptance.
