# Sensor-source intervention: isolated test evidence

Base: `d42a066f6423ade77245cc06d8e181d2e2f1cd96`, branch `fix/http-receiver-reload`. This intervention leaves `__init__.py` and `web.py` unchanged. No commit, push, deployment, live measurement injection or home-endpoint access is part of this test work.

## Environment and command

Verified installed versions: CPython **3.14.8**, Home Assistant **2026.10.2**, aiohttp **3.14.5**, pytest **9.1.1**, Ruff **0.17.1**, under isolated WSL Debian.

From the repository root on Windows:

```sh
wsl.exe -d Debian --cd /mnt/c/Users/server/cloudweatherproxy -- /mnt/c/Users/server/AppData/Local/hermes/cache/scratch/cwp-ha-testenv/bin/python -m pytest -o addopts='' -q --tb=short .
```

The pristine base was extracted separately into the agent scratch directory and executed with the same interpreter/dependencies: **44 passed in 7.22s**. The candidate full suite after implementation and verification: **94 passed in 9.91s**.

## Observed vertical RED → GREEN slices

Each production behavior below was introduced only after running its failing test. Subsequent acceptance-only coverage also verifies behaviors already introduced by these slices; those extra tests are not misrepresented as new RED cycles.

| Slice | Observed RED | Observed full-suite GREEN after implementation |
|---|---|---|
| Unsupported WU camel-case solar | `solarRadiation` incorrectly produced a multiplied lux value | 45 passed |
| Explicit source, independent of first arrival | Unselected protocol wrote sensor state in both arrival orders | 47 passed |
| Blank identity and scoped proxy trust | Unbound blank IDs and untrusted forwarded claims created entities | 49 passed |
| Semantic identities/source change | Old `rain`, `dailyrain`, `solarradiation` suffixes were reused | 50 passed |
| Real per-field expiry and invalid numbers | Humidity remained available without its field; NaN/infinity created entities | 55 passed |
| Collision rejection and protocol/origin cache isolation | Conflicting origin wrote state; both protocols occupied one cache slot | 57 passed |
| Source/configuration controls | Source/defaults missing; reconfiguration schema rejected the new fields | 68 passed |
| Canonical quantity guard | A lux-valued sample overwrote an irradiance entity | 69 passed |
| Forwarding/log privacy/runtime marker | All enabled-forwarding cases logged credential-bearing URLs; marker missing | 78 passed |
| Bound-ID conflicts and missing-ID handling | Two IDs merged into one physical binding; omitted IDs returned 500 | 81 passed |
| Protocol-independent device metadata | Switching source changed the alleged station manufacturer | 89 passed |
| Unsupported WC visibility | Unverified `vis` scaling was promoted to an HA entity | 90 passed |
| Actual HA HTTP form serialization | `probatio.to_field_list` could not serialize custom address validators | 90 passed after moving address checks to submission |
| Source display labels | Serialized choices had lowercase internal values as display names | 90 passed |
| Fractional UV | Valid WU decimal UV was dropped | 91 passed |
| Loaded vs pending settings | Diagnostics reported a newly saved timeout/binding that the existing listener had not loaded | 92 passed |

Real HA `SensorEntity` conversion tests were added before evaluating their output, then corrected for the **actual target unit-system defaults** (metric wind km/h, US pressure psi). No production conversion code was added to duplicate HA. Registry-option tests reject mm as an intensity display unit and lx as an irradiance unit, and exercise valid mm/h → in/h conversion.

## Acceptance coverage

- Both selected sources, both arrival orders, repeated alternation; unselected packets cannot write values or alter metadata.
- Eight source/forwarding-flag combinations use the real proxy dispatch/URL construction and real loopback HTTP I/O. Only the outbound destination is redirected to an ephemeral local server.
- Actual timer expiration without further packets; omitted fields and unselected traffic do not refresh it; valid return restores availability; unload cancels timers.
- Named multi-station isolation, blank/whitespace IDs, exact nonempty token preservation, duplicate/colliding identities, trusted/untrusted/duplicate/multiple-hop/malformed XFF, unsupported forwarding headers.
- Explicit physical association preserves compatible anonymous IDs/device identity across source changes; incompatible rain dimensions remain distinct. Real entity-platform tests verify upstream counter-ID reattachment and custom-name preservation.
- Existing registry records and custom names remain untouched. Registry mutation/removal methods fail the test if integration callbacks invoke them.
- Metadata remains stable after listener recreation/reload. Official WC scaling examples, WU rolling rain and lowercase irradiance, fractional UV and nonfinite/invalid/missing values are covered.
- Real config-flow creation/reconfigure schemas, target HA form serialization, validation, actual entry-data updates and reload scheduling are exercised. Full Core reload scheduling itself is substituted in the flow test; the original lifecycle suite separately crosses real HA entry-state boundaries.

## Quality checks and limits

- Ruff baseline comparison: **9 baseline findings, 9 candidate findings, 0 introduced**. All nine are pre-existing D100/D103 docstring findings in the vendored conversion/sensor tests. Repository lint policy was not weakened.
- Added tracked lines and every untracked deliverable were scanned for hardcoded credentials, shell/eval/deserialization/SQL hazards and private deployment payload identifiers: no actionable findings. A broad credential-assignment regex matched the privacy test's assertion that `PASSWORD=` is absent from logs; that exact assertion was inspected and classified as a benign match.
- Production Python AST and changed JSON validation passed. Tracked `git diff --check` and untracked whitespace validation passed.
- Tests retain the first intervention's explicit entity-platform/state-write seam. This is **not** a full Core/Recorder/frontend boot or a live installation verification; no Recorder database or statistics repair is attempted.
- WU camel-case solar and the particular station's disagreement between daily rain counters remain device-specification blockers, not guessed conversions. The final source preference remains an owner decision.
- Independent review, commit, publication and deployment belong to the parent agent and are not claimed here.
