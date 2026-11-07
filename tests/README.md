# HTTP receiver lifecycle regression tests

Run on Linux with Python 3.14 (including WSL Debian):

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test-ha2026.10.txt
.venv/bin/python -m pytest -v
```

The original `requirements.txt` targets HA 2025.12.5 and pins pycares<5;
HA 2026.10.2 requires aiodns 4 / pycares>=5. Do not combine those files.
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

Caller cancellation is deferred until the owned unload transaction returns its
result to HA. Accepted requests share a 30-second monotonic deadline beginning at
the first unload or stop operation. They retain callbacks and sessions during that
interval. Outstanding request tasks are cancelled once at expiry, with one second
for settlement. Cleanup has a five-second limit and a one-second cancellation
settlement limit. Neither wait uses an unbounded cancellation gather.

If stopping a retired listener fails after platform teardown, the receiver remains
inactive and DomainData explicitly owns the quarantined listener. Errors are logged,
and the unload hook returns false. HA 2026.10.4 retains runtime data and enters
`FAILED_UNLOAD`, which normal reload cannot recover; restart is required. The setup
hook can retry cleanup before creating a replacement. Unfinished cleanup remains
tracked in DomainData, with its original deadlines. A domain-level HA stop callback joins
any in-progress setup/unload transaction and then drains and closes the active
listener or retries quarantine, without requiring another setup. Shutdown never
re-enables ingestion if a pending platform setup completes. A cleanup lock keeps
shutdown and an in-progress cleanup from stopping the same listener concurrently.
`test_shutdown_cleanup.py` exercises real `HomeAssistant.async_stop`, failed unload
and setup, accepted HTTP requests, shutdown during drain/setup, and repeated stop
failure. If the shutdown cleanup also fails, ownership
is retained and the failure is logged; closure is not guaranteed for a permanently
failing stop operation. This is one shutdown retry, not a periodic retry service.

`test_drain_deadline.py` covers both HTTP protocols, forwarding, permanent blocked
requests, uncooperative cancellation, bounded cleanup, quarantine recovery, real
ConfigEntry unload and ConfigEntries reload concurrent with real HA stop. One
isolated synthetic HTTP test uses the production 30-second deadline and real clock.
Other deadline tests shorten constants to exercise the same code deterministically.

## Loaded-code identification

Config-entry diagnostics include `receiver_lifecycle` with implementation
`drain30.1`, API version 2, the actual GET route count from aiohttp's public
router API, active/accepting flags, in-flight leases, the shared deadline and
cleanup ownership. The permanent
registry retains no ConfigEntry object and survives unload/removal, so the route
count remains two across reloads. The manifest version is
`2026.2.3+sensorsource.6`. Changed Python modules require a Core restart to load.
