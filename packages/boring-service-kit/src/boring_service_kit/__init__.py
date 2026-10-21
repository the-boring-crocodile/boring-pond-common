"""boring-service-kit: what every boring MCP service shares.

- `outcomes`, `calllog`, `tools`: the tool conventions (outcomes as data, one
  compact result, lean schemas, strict arguments, the call log).
- `pond`: the service side of the Pond's service contract (boring-crocodile `docs/SERVICES_AND_PIRATES.md`).
- `http`: independent HTTP hosting with OAuth introspection, access keys or both.
- `keys`: service access keys and `boring-service-keys`.

Services depend on this package's releases; `pond` needs boring-registry.
"""

__version__ = "0.7.5"
