# boring-deploy

How every Pond component is installed, updated, rolled back and uninstalled.
One engine (standard library only) for all of them: a component's release
artifact carries a copy of it plus the component's own steps. This page is
the one place that explains it; components document only their own settings
and checks.

## Using a release

Unpack the artifact anywhere and run its commands as root:

    sudo RELEASE/deploy [HOME] [--check] [--backup | --no-backup]
    sudo RELEASE/uninstall [HOME] [--delete-data] [--yes]      # components with an installer

HOME defaults to the component's line in `/etc/boring-pond/homes.json`, then
to the release's default (for example `/opt/boring-crocodile`).

The host needs `python3` 3.11 or newer to run `deploy` and `uninstall` (the
component itself runs on the Python its release brings). On an older one
they stop with one line before anything else:
`python3 3.11 or newer is needed to run this; this is python3 3.10`.

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
is left alone. docker: the new image goes into `.env` as always, but
`compose up -d` runs only when a container of the stack runs (or restarts);
a stack stopped with `docker compose stop` or `down` (no container, or only
exited ones) stays stopped and runs the new image at its next start. When
nothing runs, the deploy says `site is stopped: not started`, skips start and
smoke, and records the release as installed, not started. A first install
starts what it made (its units, its stack).

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
2. a component's own lock, taken by its deploy right after: the Crocodile's
   site lock (`/etc/boring-crocodile/site.lock`), so a deploy of the Crocodile
   and a site change (setup, `--site-extend`, `pirate-add`/`pirate-remove`,
   task-policy, uninstall) refuse each other instead of overlapping;
3. `homes.lock`.

The Crocodile's site commands take only the site lock, never a deploy lock.
A Pirate install calls `pirate-add`, which takes the site lock while the
Pirates' own operation lock is held: that is the same order. A lock that is
held refuses at once (except `homes.lock`, which waits); there is no retry.

### Rollback

Rollback is the previous release's own deploy: `sudo HOME/previous/deploy`
(container components: `HOME/release.previous/deploy`). Nothing is rolled back
automatically: a failed smoke says so and names the command. Only one previous
release is kept.

Rolling back out of a restart loop can find systemd's start limit hit: the
switch stops with `systemctl restart ... exited 1` and the component's units
are `failed`. Clear their counters with `sudo systemctl reset-failed` (the
component's unit names, wildcards allowed) and run the rollback again.

### First install and uninstall

A component with an installer installs itself on its first deploy (step 0), so
there is one command for both. Its settings exist only for that first install;
afterwards they are refused (change the component's configuration instead).
Some components install several named instances (`--name`, a Pirate each).

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
makes the component look installed goes last.

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

A component's `scripts/build.py` calls `boring_deploy.artifact.assemble(out,
release, steps, files, install=None)`. `steps` is a module with optional
functions: `check(ctx)`, `smoke(ctx)`, `units(ctx)` (venv), `healthy(ctx)`,
`data_generation(ctx)`, `backup(ctx, directory)`. `install` is a module with
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
