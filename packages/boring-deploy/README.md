# boring-deploy

The shared Linux deployment engine for Crocodile, Pirates and Services.
Each release carries this engine plus its component's own installation and
health checks. The common sequence gives an operator the same way to inspect,
switch and recover releases across components; component guides own their
settings and the work a restart interrupts. Windows pondNT has its own
[installer](https://github.com/the-boring-crocodile/boring-pondnt/blob/main/docs/INSTALL.md).

Start with [Using a release](#using-a-release). For an interrupted update, read
[What a deploy does](#what-a-deploy-does) and [Rollback](#rollback);
[First install and uninstall](#first-install-and-uninstall) explains retained
data. [Building a release](#building-a-release-for-component-authors) is for
component authors.

## Using a release

Unpack the artifact anywhere and run its commands as root:

    sudo RELEASE/deploy [HOME] [--check] [--backup | --no-backup]
    sudo RELEASE/uninstall [HOME] [--delete-data] [--yes]      # components with an installer

HOME defaults to the component's line in `/etc/boring-pond/homes.json`, then
to the release's default (for example `/opt/boring-crocodile`). Deploy checks
HOME and its ancestry before it inspects or prepares the installation. Use
root-owned directories that other users cannot write, without symlink
components. Keep the service data permissions described by the component.

The fixture harness can set `BORING_DEPLOY_TEST` to bypass the effective-root
check. This supplies no permissions. Leave it unset for host deploy and
uninstall commands.

The host needs `python3` 3.12 or newer to run `deploy` and `uninstall` (the
component itself runs on the Python its release brings). On an older one
they stop with one line before anything else:
`python3 3.12 or newer is needed to run this; this is python3 3.10`.

Linux artifacts record `architecture` (`x86_64`). Venv-runtime artifacts also
record `glibc_min`: they require host glibc 2.39 or newer (for example Ubuntu
24.04 or Debian 13). Check `getconf GNU_LIBC_VERSION` before downloading one.
Docker-runtime artifacts use their container's libc and declare no host glibc
floor. Both `deploy --check` and `deploy` inspect the declared requirements
before first-install planning or changes, naming both host and required
versions on refusal. Older artifacts without a field retain their original
behavior for that check. Wheel downloads enumerate every compatible manylinux
x86_64 tag from the floor through glibc 2.17, then the legacy aliases.

**The unpacked release directory is code that runs as root.** `deploy`
imports its engine and steps from `deploy.d/` before anything is verified,
and the release is copied into HOME from there. Unpack it where only root
(or the operator who runs `sudo`) can write: not a shared or world-writable
folder such as `/tmp`, not a folder another account owns. The manifest check
(step 1) catches damage, such as a truncated download or an edited file; it
does not catch tampering, because whoever can write the directory can
rewrite `MANIFEST.sha256` as well. The copy in HOME is checked against the
manifest again before it is used.

Unpack the artifact itself, as root, rather than using a folder a build left
behind: `sudo tar xzf ARTIFACT.tar.gz -C /root/releases`. The copy in HOME
is made readable for the component's own accounts (directories and
executables 0755, other files 0644) whatever modes the unpacked files have.

### What a deploy does

The diagram follows a running installation through a successful update.
A stopped installation skips start and smoke, as described below.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/deploy-update-dark.svg">
  <img alt="How a deploy updates a component, in four lanes: the operator runs deploy; in the unpacked release it verifies each file (1); in HOME it copies the release into releases/VERSION (2); back in the release it runs the component's check (3). Then the point of no return: in HOME it switches current and previous (4); in what runs it restarts and waits for the units (5) and runs the smoke check (6); in HOME it writes one line in DEPLOYED (7). Before the line a failure leaves the old release running; after it, roll back with HOME/previous/deploy." src="docs/deploy-update.svg">
</picture>

Each step must pass before the next. Nothing before step 4 changes what
runs; step 2 writes the new release into HOME beside the running one
(`HOME/releases/VERSION`, or `HOME/release.new` and the image
`IMAGE:VERSION`), and an approved first install (step 0) changes the host:

| Step | |
|---|---|
| 0 install? | Components with an installer: looks at the host. Installed: go on. Not installed: plan a first install and ask (`--install` without a terminal; settings as flags). Irregular: stop and list what is wrong. |
| 1 verify | Files against the manifest (damage, not tampering); HOME belongs to this component; required components installed in the right versions; data readable. |
| 2 prepare | The new release beside the running one. |
| 3 check | The component's own check, against the new release, nothing live. |
| backup | Only when the data format changes: asked, or `--backup` / `--no-backup`. |
| 4 switch | The running release becomes `previous`, the new one `current`; restart what runs. A restart ends the component's work in progress (what that means: the component's own docs). |
| 5 start | Wait until it runs. |
| 6 smoke | The component's own end-to-end check. |
| 7 record | One line in `HOME/DEPLOYED`; print the rollback command. |

`--check` is read-only: it verifies the unpacked release and the host (steps
0 and 1: manifest, HOME, required components, data generation, what a first
install would do) and writes nothing, not even a lock file. It does not
prepare the release and so cannot run the component's own check (step 3):
that happens in the deploy itself, still before the switch. A first install
with `--check` stops after its plan.

A stopped installation stays stopped across an update, for systemd units and
Docker stacks alike. venv: the switch restarts only units that run (or
failed); an inactive unit, such as one stopped with `systemctl disable --now`,
is left alone. docker: the new image goes into deploy-owned `boring-image.env`, but
`compose up -d` runs only when a container of the stack runs (or restarts);
a stack stopped with `docker compose stop` or `down` (no container, or only
exited ones) stays stopped and runs the new image at its next start. When
nothing runs, the deploy says `site is stopped: not started`, skips start and
smoke, and records the release as installed, not started. A first install
starts what it made (its units, its stack).

The operator's `.env` is kept, including its mode and other values. On the
first update, migration removes only exact legacy `BORING_IMAGE=` lines,
preserving every other byte and the file's ownership/mode; an empty file
stays. New installs do not create `.env`. Uninstall keeps it too.
Compose commands load an existing operator `.env` first, then the deploy's
`boring-image.env`, so the managed image wins without hiding operator values.
For manual commands, use `docker compose --env-file boring-image.env ...`;
when `.env` exists, put `--env-file .env` before that flag. The stopped-site
start hint includes the right files automatically.

A Docker stack can hold more than the component: a front end beside backends
it does not build (the services' MCP ferry and its ha-mcp, say). The steps'
`units(ctx)` then names the compose services this deploy owns; whether the
installation runs, `compose up -d` and the wait ask about those services only
(Compose still starts their `depends_on`). The other services are the
operator's: started, updated and watched with `docker compose` directly. A
step module without `units` (every Docker component so far) gets the whole
stack, as before.

Before the switch writes anything, the deploy refuses a name that is not a
service of the stack (`docker compose config --services`), and an unnamed
service that also uses `${BORING_IMAGE}`: it would keep running the old image
while `boring-image.env` names the new one. The wait covers the named services only, not
the `depends_on` Compose starts with them; a front end that needs a healthy
backend says so with `condition: service_healthy`, which `up -d` honours.
A rollback to a release whose engine is older than 0.7.8 ignores `units` and
brings up the whole stack.

### One operation at a time

A deploy or uninstall of a HOME holds that HOME's operation lock
(`/run/boring-pond/home-...lock`) from before it reads the installation to
after its record. A second deploy or uninstall of the same HOME does not wait:
it stops with `another deploy or uninstall of HOME is in progress` and changes
nothing; run it again once the first has finished. `--check` refuses the same
way while one runs. The host-wide `homes.json` is changed under its own lock
(`/run/boring-pond/homes.lock`), held only for its read and write, so installs
of different components cannot lose each other's line.

Lock order, outermost first, so two holders never wait for each other:

1. the HOME's operation lock (deploy, uninstall);
2. the component's own lock, if its mutating deploy declares one;
3. `homes.lock`.

Component guides own their additional locks and cross-component calls.
Those calls must keep this order too. A held operation or component lock
refuses at once; `homes.lock` waits. There is no automatic retry.

### Rollback

Rollback changes the software being run. It does not undo work that an agent or
service already performed, and it does not restore data automatically. If a
new release changed the data format, the older release may refuse it: restore
the compatible backup through the component's recovery procedure before trying
that older software. A retained release and a data backup solve different parts
of recovery.

Rollback is the previous release's own deploy: `sudo HOME/previous/deploy`
(container components: `HOME/release.previous/deploy`). Nothing is rolled back
automatically: a failed smoke says so and names the command. Only one previous
release is kept. A component can replace the printed command with its own
recovery procedure when restoring compatible files needs more than a runtime
switch; follow that guidance on both a completed deploy and a failed switch.

Rolling back out of a restart loop can find systemd's start limit hit: the
switch stops with `systemctl restart ... exited 1` and the component's units
are `failed`. Clear their counters with `sudo systemctl reset-failed` (the
component's unit names, wildcards allowed) and run the rollback again.

### First install and uninstall

A component with an installer installs itself on its first deploy (step 0), so
there is one command for both. Its settings exist only for that first install;
afterwards they are refused (change the component's configuration instead).
Some components install several named instances (`--name`, a Pirate each).
For these, `--install` adds the named instance even when others are installed;
the first-install restriction applies to that instance.

Uninstall stops the component and removes what its install created, but never
a home, state or configuration: it lists them as no longer live, for a person
to delete. It first stops the component and goes on only once it is
stopped; a failed stop leaves everything in place for a rerun. Once nothing
of the component is left, its line leaves `homes.json`.

`--delete-data` also removes the component's account. A removed account's
number can be given to a later account, which would then own every file the
old one left. So, right before `userdel`, each kept folder the account owned
(a Pirate's home and state, a service's `state/` and `config/`) is sealed:
changed to `root:root`, mode 0700, through a descriptor opened without
following symlinks (`boring_deploy.install.seal`). What is inside keeps its
old owner but is out of reach. A symlink, a mount point, something not a
folder or a folder of another account stops the uninstall before any change
to them and the account is kept. An uninstall that stopped part-way (a
failed stop, seal or `userdel`) is finished by running it again: what
makes the component look installed goes last. Components may also recognize
retained cleanup after a completed uninstall: Pirates accepts a later
`uninstall --name NAME --delete-data` to seal what remains and remove its
account. This does not delete the retained files.

To use sealed data again, a person looks at it and hands it over on purpose
(for example `chown -R NEWACCOUNT: FOLDER` after checking what is inside), or
moves it away, before installing again. A reinstall gives back only the top
folders it makes itself (a service's `state/`, a Pirate's state folder), not
what is inside them, and not a Pirate's home.

### Records on the host

- `HOME/DEPLOYED`: one line per deploy, install and uninstall: time, action,
  version, previous version, who.
- `HOME/COMPONENT`: which component a HOME belongs to; a deploy into another
  component's HOME is refused.
- `/etc/boring-pond/homes.json`: component -> HOME, written by the first
  install. A deploy pointed at another folder while the recorded one exists is
  refused as a likely typo.

## Building a release (for component authors)

Root-run step commands use `ctx.run(...)`. It calls the shared
`root_child(command, user=None, spawn=None, **options)` boundary: host tools
resolve only in `/usr/sbin:/usr/bin:/sbin:/bin`, Python runs with `-I -B`,
and every child starts in `/`. Python console-script interpreters get the
same isolation. The child environment uses that fixed system PATH.
Call `root_child` directly for another root-owned process owner; pass
`spawn=subprocess.Popen` when it needs a process handle. That caller keeps
its own capture, timeout and process-group cleanup policy.

Restricted account provisioning uses `create_system_account(name, home, comment,
ctx.run)`: a fresh system account, private primary group and nologin shell,
with no home creation. Existing names, groups and home paths are refused.
It observes the assigned `AccountIdentity(uid, gid)` after creation; the
component retains those IDs to detect a replaced account. Later
`system_account(name, home, comment, expected)` checks those IDs, the home,
shell, comment and exclusive group membership. Observation does not authorize
adopting an existing account. `lock_system_account(..., expected, ctx.run)`
rechecks that identity, locks its password and expires the account while
keeping every file. The component still owns revoking SSH keys and confirming
that existing sessions are closed; this helper neither stops sessions nor
deletes accounts, groups or data.

File and directory install settings use `root_input(path, directory=False)`.
It opens every path component without following links and checks the whole
input tree: root ownership, no other writer, regular files/directories only,
and no multiply linked files. Root-owned sticky ancestors such as `/tmp`
are allowed; the input itself must still be protected. Stage inputs first.
Absolute paths may contain parent references such as `../credentials`: each
component is checked in order, so a symlink before `..` is still refused.
The context keeps its descriptor open. Use `read_text()` or `copy_to(new_path)`
for a file instead of reopening its name; copying streams into a new 0600
file and refuses an existing target. `describe()` returns owner/mode lines
for the install plan and includes no content. Components must use this
boundary for their later reads too, including files named by configuration.

A component that keeps its account on removal can set `DELETE_DATA_HELP` in
its install steps. That text owns the --delete-data help and confirmation;
the component's uninstall callback still owns the account/data behavior.

Configuration-only components declare `runtime: files` in RELEASE.json.
They use the same verified release copies, current/previous links, operation
locks, install hooks and rollback as venv releases, while preparing no Python
application or container. Their deployment code runs on the launcher's host
Python. They supply their own configuration checks and completion wording;
there is no component process to start. No other component needs to depend on
a configuration-only component such as the optional tunnel.

A component's `scripts/build.py` calls `boring_deploy.artifact.assemble(out,
release, steps, files, install=None)`. `steps` is a module with optional
functions: `lock(ctx)`, `check(ctx)`, `smoke(ctx)`, `units(ctx)` (systemd units or Compose services), `healthy(ctx)`,
`preflight(ctx)`, `data_generation(ctx)`, `backup(ctx, directory)`,
`way_back(ctx, previous)`, `completion(ctx)`. The completion hook returns a
status string for the final record line after a successful deploy; `None` keeps
the normal live/stopped wording. It is read-only and does not skip start or smoke.
The way-back hook reads the retained release and returns its recovery guidance
as a string; `None` keeps the normal deploy command.
It runs only when a distinct previous release exists, after the switch,
on both success and failure. It must not change the host.
`lock(ctx)` returns a context manager for component-owned state. A mutating
deploy holds it inside the HOME operation lock through its final record;
`--check` does not acquire this additional lock.
`preflight` returns a list of problem strings, read-only, after manifest and
requirement checks and before first-install work in both deploy modes. All
problems are printed before the deploy stops; an empty list continues.
Components without it keep their existing install and data-generation order.
`check_dependency(component, spec, optional=False)` shares the required-component
HOME lookup and version comparison. With `optional=True`, an absent peer is allowed;
a recorded HOME or runtime marker without a version still fails. Components own
when their optional peers must match, including checks before starting a stopped site.
This check reads installed state; it does not lock other components' HOMEs or
coordinate concurrent component updates. Follow the component's ordered update procedure.
`install` is a module with
`look(ctx)`, `SETTINGS`, `ready`, `plan`, `install` and `uninstall`
(`boring_deploy.install` documents each). The engine's docstring
(`src/boring_deploy/__init__.py`) is the reference for the step contract.

`artifact.standalone_python(uv, version, dest)` packs the python-build-standalone
CPython that uv installs. Those install_only builds leave out the licence texts
of the libraries linked into CPython (OpenSSL, SQLite, libffi, ...), so the
package keeps them per build in `python_notices/VERSION+TAG/` and ships them
as `python/licenses/`. A build whose CPython has none there stops with the
command that fetches them: `python scripts/python_notices.py VERSION TAG` in
boring-pond-common (from the matching full build, checked against the
release's SHA256SUMS).
