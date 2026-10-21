# Pond Common

Authoritative shared Python packages for The Boring Pond. Each package keeps its
own distribution name, version and dependency list; there is no umbrella runtime
dependency.

All packages share one version, currently in [VERSION](VERSION); see the
[changelog](CHANGELOG.md).

All of it — code, tests, docs — was written by AI (OpenAI's models and Anthropic's Claude); Wald decided what to build, tested it, ran it and had the last word ([all of it is AI](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md#all-of-it-is-ai)).

| Package | Consumers |
|---|---|
| boring-registry | Crocodile, Pirates, services, pondNT |
| boring-file-core | Crocodile, Pirates, pondNT |
| boring-service-kit | Crocodile, services, pondNT |
| boring-worker-kit | Crocodile, Pirates (Linux only) |
| boring-deploy | every component's release: deploy, install, rollback, uninstall ([how it works](packages/boring-deploy/README.md)) |

boring-task-state currently has only Crocodile as a consumer and stays there.
Common packages must not import a host or service application.

## Development

    python scripts/check.py                 # versions, build, all tests on the built wheels
    python scripts/check.py --python 3.10   # same on another interpreter (uv fetches it)
    python scripts/build.py                 # wheels + sdists into dist/, with SHA256SUMS
    python scripts/version.py 0.7.0         # set the lockstep version everywhere

The tools need [uv](https://docs.astral.sh/uv/) on PATH (or `UV=/path/to/uv`).
For quick iteration, install the packages editable into a development
environment and run one package's tests, for example
`python -m pytest -q packages/boring-registry/tests`.

## Consumers

Crocodile, Pirates, the services and pondNT declare a version range of these
packages and pin the exact wheels by hash in their own lock files. Until
Common is on a package index, they resolve it from a directory of these
wheels (`--common-wheels`), built from the Common release each repository
names; the build is reproducible, so the hashes match.

To get that directory, download the wheels attached to the Common release,
or build them from the commit a consumer's `.common-ref` names (with
[uv](https://docs.astral.sh/uv/) on PATH):

```bash
git clone https://github.com/the-boring-crocodile/boring-pond-common
cd boring-pond-common
git checkout "$(cat ../boring-crocodile/.common-ref)"
python3 scripts/build.py --out ../common-dist
```

Then `--common-wheels ../common-dist` in the consumer's scripts.

[The Pond](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md)

## Licence

Code under the GNU AGPL, version 3 or later; documentation under CC BY-SA
4.0. What you make with the tools is yours. Details and the exact terms:
[COPYING.md](COPYING.md).
