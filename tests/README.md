# HTTP receiver lifecycle regression tests

Run on Linux with Python 3.14 (including WSL Debian):

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test-ha2026.10.txt
.venv/bin/python -m pytest -v
```

The upstream `requirements.txt` targets HA 2025.12.5; the separate test
requirements target HA 2026.10.2. Use separate environments rather than
combining those dependency sets.
The initial reload regression also passed against the original HA 2025.12.5
environment during development; the complete suite is verified against HA 2026.10.2.

`test_receiver_lifecycle.py` uses real `HomeAssistant`, `ConfigEntries`,
`ConfigEntry`, `HomeAssistantHTTP.register_view`, HA's view/auth wrapper, the
integration's real sensor setup and listener, and real aiohttp TCP requests.
Platform forwarding/unloading is substituted at the HA platform-manager seam;
entity state writes are recorded instead of booting a full entity platform.
The unload seam invokes the real entry unload callbacks. Authentication middleware
supplies only a synthetic authenticated flag; tests verify that HA still rejects
unauthenticated requests. This is not a full running Home Assistant installation.

Proxy tests use actual ClientSessions/connectors with the unchanged cloud resolver
configuration, forwarding only to an ephemeral loopback server. Test station IDs,
keys and DNS addresses are synthetic; no external weather API is contacted.

## Baseline RED reproduction

Copy the regression test to an extracted pristine `2026.2.1` tag checkout
(commit `6d72af475ba3feaad928e5f494cc0813d3e5f04e`), then use the same environment:

```sh
python -m pytest tests/test_receiver_lifecycle.py -k pre_start -v --tb=short
python -m pytest tests/test_receiver_lifecycle.py -k reload_dispatches -v --tb=short
```

On pristine code, both pre-start endpoint cases return HTTP 200 but fail because
`entry.runtime_data.known_sensors` remains empty: routing calls the retired listener.
Both running-server reload cases fail with `Cannot register a resource into frozen
router`. On the fix, both scenarios pass without changing private router structures.
The in-flight tests additionally proved RED before the request-draining fix: unload
completed early and removed the sensor callback while a request was still running.

The legacy library HTTP test had two independent baseline harness defects: fixture
paths depended on the working directory, and recorded paths omitted the application's
public URL mounts. The small test-only correction fixes those and removes an unused
fixed-port server; parsing code and fixture data are unchanged.

## Cancellation and cleanup failure boundaries

`test_real_entry_lifecycle.py` and `test_real_entry_cancellation.py` additionally
exercise the installed HA ConfigEntry setup/unload wrappers and their state
transitions. Platform orchestration remains substituted at the seam described
above; these are not full Core boot or live-installation tests.

Unload is an atomic transaction: caller cancellation, including repeated
cancellation during draining or resource cleanup, is consumed until the transaction
finishes so HA can complete its own state transition and runtime-data removal.
Cancellation of an individual HTTP request still releases its listener lease.
There is no independent unload deadline: a request or cleanup operation that never
finishes can delay unload/shutdown despite caller cancellation. The tests establish
completion once blocked work is released, not bounded shutdown latency.

If stopping a retired listener fails after platform teardown, the receiver remains
inactive and DomainData explicitly owns the quarantined listener. Errors are logged,
and a later setup retries its cleanup before creating a replacement. No cleanup is
left running as an untracked background task. A domain-level shutdown retry is not
implemented: if there is no subsequent setup, a quarantined session may remain open
until process termination. This is a known failure-path limitation, not proof that
all resources have closed.

## Loaded-code identification

Config-entry diagnostics include `receiver_lifecycle` with implementation
`reloadfix.1`, API version 1, the actual GET route count from aiohttp's public
router API, active/accepting flags and the number of in-flight leases. The permanent
registry retains no ConfigEntry object and survives unload/removal, so the route
count remains two across reloads. This contribution retains upstream's manifest
version; release versioning is left to the maintainer. Deploying changed Python modules still needs
a separately authorized initial Core restart; these tests do not deploy or restart HA.
