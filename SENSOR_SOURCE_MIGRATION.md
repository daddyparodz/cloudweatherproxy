# Sensor source and physical units (`sensorsource.1`)

This feature does not choose which protocol your particular station implements correctly. Select **Wunderground** or **Weathercloud** explicitly in the integration's **Reconfigure** form. In Italian the field is **Sorgente dei sensori**. The selection is global for the integration, not per station. There is no automatic fallback and arrival order never selects the sensor source.

Both receiver routes remain active. **Proxy Weather Underground** and **Proxy Weathercloud** still independently control forwarding to each cloud service. Discarding a packet for HA sensor identity/source does not discard its cloud forwarding. Authentication and the HTTP reload lifecycle are unchanged.

## Configuration

| Entry setting | Default | Meaning |
|---|---|---|
| `sensor_source` | `wunderground` | `wunderground` or `weathercloud`, independently of both proxy flags. Old entries without this setting use the deterministic Wunderground default until explicitly reconfigured. |
| `sensor_timeout` | `900` | Seconds without a fresh valid value before **that field** becomes unavailable. Allowed range 1–86400. Use at least 900 seconds for Weathercloud's usual ten-minute cadence; allow more for packet loss. |
| `trusted_sensor_proxies` | empty | Comma-separated numeric IP allowlist of the actual TCP transport peers permitted to supply one numeric `X-Forwarded-For` address. No DNS names or CIDRs. |
| `anonymous_station_address` | empty | Explicit numeric address representing **one physical station**. Required for empty/whitespace-only IDs. Also establishes the only supported cross-protocol physical association. Despite the historical setting name, it may associate nonempty protocol IDs from that station too. |
| `legacy_anonymous_binding` | `false` | Explicitly associate that bound physical station with the old anonymous device identifier and compatible `-<sensorname>` unique IDs. Requires `anonymous_station_address`. Enable only after verifying the existing anonymous device belongs to that physical station. |

These are integration settings, not additions to Home Assistant's global `http` configuration. They do not change Caddy, DNS, services, authentication or HA's global proxy trust. Reconfiguration saves the entry and reloads it; it never closes the live proxy session before the receiver drains.

Example using documentation-only addresses: if a verified proxy has TCP peer address `192.0.2.10` and the physical station is `192.0.2.20`, set `trusted_sensor_proxies` to `192.0.2.10` and `anonymous_station_address` to `192.0.2.20`. Enable legacy association separately only if the existing anonymous device has been verified. Do not copy these example addresses into a real installation.

Identity uses the transport peer, **not** `request.remote` or an unchecked header. An untrusted forwarded claim, duplicate XFF header, multiple-hop XFF list, malformed numeric address, `Forwarded`, or `X-Real-IP` causes identity rejection. A trusted proxy must provide the supported single XFF hop. Rejected identity still receives the normal local response and follows the unchanged forwarding flags. This deliberately supports only the verified single-proxy arrangement, not arbitrary proxy chains.

## Availability

Each accepted field has its own cancellable event-loop timer. Missing fields, invalid numbers, NaN/infinity and the unselected protocol do not replace values with zero and do not renew timers. The timer writes unavailable even if no further packets arrive. A fresh selected-source value restores that field. Timers are cancelled on entity removal and successful entry unload. No previously unseen selected-source field is invented when that source is absent.

## Quantities and canonical native units

Metadata is chosen by semantic quantity and checked against the canonical incoming native unit. It is not inferred anew from whichever packet arrives first. Home Assistant's `SensorEntity` performs supported display conversions and honors compatible user-selected units; the integration does not implement another display-conversion layer.

| Protocol input | HA quantity / unique-ID suffix | Native unit | State class |
|---|---|---|---|
| WU `rainin` | Rain past 60 minutes / `rain_1h` | mm, inches × 25.4 | `measurement` |
| WC `rainrate` | Rain intensity / `rain_intensity` | mm/h, encoded value ÷ 10 | `measurement` |
| WU `dailyrainin` | Daily precipitation / `wunderground_dailyrain` | mm, inches × 25.4 | `total_increasing` |
| WC `rain` | Daily precipitation / `weathercloud_dailyrain` | mm, encoded value ÷ 10 | `total_increasing` |
| WU lowercase `solarradiation` | Solar irradiance / `irradiance` | W/m², unchanged | `measurement` |
| WC `solarrad` | Solar irradiance / `irradiance` | W/m², encoded value ÷ 10 | `measurement` |

Temperature is native °C, pressure hPa, wind speed m/s, humidity %, directions degrees and UV a dimensionless index. WU fractional UV values are retained; WC UV tenths are decoded. HA display units can differ from these native units. In the tested HA version the metric wind default is km/h and US customary pressure default is psi; compatible registry options can select alternatives.

WU `rainin` is a rolling **amount**, not an instantaneous intensity. It can fall as rain leaves the past-hour window, so it is not `total_increasing`. It is never equated with WC rain rate. The two daily-counter series remain separate even for an explicitly bound station. No guessed offsets, cross-protocol counter continuity, synthetic resets or rain multipliers are applied. WC's documented daily meaning does not establish that a particular firmware sends the documented quantity correctly.

WU camel-case `solarRadiation` has **no verified device-specific unit contract** here. It is ignored for HA entities and forwarded unchanged when enabled. It is not interpreted as lux, multiplied by 1000, treated as an alias of lowercase `solarradiation`, or converted to W/m². No lux source is currently supported, and there is no universal lux-to-irradiance conversion. A matching numeric pair across protocols alone does not establish the undocumented field's contract. The old unverified Weathercloud `vis` field is not promoted to a HA quantity.

Home Assistant's precipitation and precipitation-intensity device classes provide different conversion sets (distance and speed converters in HA 2026.10.2). mm↔mm/h and lx↔W/m² are not offered as interchangeable units. There is no custom `PrecipitationIntensityConverter` dependency.

## Identity and multiple stations

- Nonempty IDs retain their exact received tokens; whitespace is stripped only to detect an empty ID. Internal identities include protocol, exact ID and verified origin address, encoded as an opaque namespace. Matching WU/WC text IDs alone do **not** establish the same physical station.
- Several nonempty-ID stations may coexist, all using the globally selected protocol. Reusing one protocol ID from a different origin is rejected and counted during that loaded listener's lifetime. Origins are also part of persistent unique IDs, so a listener recreation cannot silently merge the two origins.
- Only the explicitly configured physical station address can provide an empty ID. Other empty-ID origins are rejected, never merged into the old empty device. A proxy transport address alone cannot stand in for multiple anonymous downstream stations.
- One bound address represents one physical station. Conflicting protocol IDs claiming that association in one loaded listener are rejected rather than merged. Multiple stations sharing the same indistinguishable address and blank ID cannot be resolved from these payloads. A second anonymous station needs a separate, verifiable identity arrangement; this UI does not provide multiple cross-protocol bindings.
- Without legacy association the bound station has an address-derived physical namespace shared across protocols. With explicit legacy association its device identifier remains `(cloudweatherproxy, "")`; compatible fields keep their old `-temperature`, `-humidity`, `-windspeed`, etc. unique IDs.
- Changing a bound address changes physical identity. Do not move the legacy association to another physical station. Removing the binding or changing an unbound station's origin/protocol intentionally creates distinct identities.

## History-preserving migration

No Recorder/statistics/history database is opened or repaired. No entity-registry delete, disable, rename or identity-rewrite operation is performed by this integration. No dashboards or Alexa settings are changed.

For a verified legacy anonymous station:

1. Select the sensor protocol deliberately and configure the verified proxy/physical binding as needed.
2. Opt into `legacy_anonymous_binding` only after confirming the old anonymous device is that station.
3. Compatible quantities keep the same unique IDs and device identifier. HA's usual entity-platform registration can reattach those entities without a custom registry migration.
4. **Do not repurpose** `-rain` (old historical mm despite the Rain Rate label) or `-solarradiation` (old lux history) as mm/h or W/m². New `-rain_1h`/`-rain_intensity` and `-irradiance` entities are intentionally distinct. Daily, weekly and monthly counters also get source-specific suffixes rather than pretending prior mixed-source counter history was clean.
5. Old incompatible registry/history records remain untouched and inactive; HA's normal unload/removal behavior makes old runtime entities unavailable. New semantic entities can receive new entity IDs chosen by HA. Existing dashboards/Alexa remain unchanged and may still refer to inactive legacy entities until the owner changes them deliberately.

For an unbound legacy **nonempty** station ID, the old protocol-agnostic identity cannot be proven safe to reuse. New protocol/origin namespaces are intentional. Automatic nonempty-ID registry migration is not implemented.

## Loaded-code proof and validation scope

The manifest version is `2026.2.1+sensorsource.1`. Diagnostics retain `receiver_lifecycle.implementation = reloadfix.1` and add `sensor_source.implementation = sensorsource.1`, loaded selected source, per-source acceptance/rejection counts, accepted field count, timeout, and binding/trust-presence information. Diagnostics omit raw station IDs, addresses, keys, measurements and historical free-text logs. Counters reset when the listener is recreated.

The isolated tests use real HA 2026.10.2 classes, real `SensorEntity` conversion/registry-option handling, real event-loop timers and ephemeral loopback HTTP forwarding. As in the first intervention, the entity-platform orchestration/state-write seam is substituted; this is not a complete Core/Recorder/frontend boot or a live-installation verification. Source preference and device-specific daily-rain/solar semantics require owner/firmware confirmation.

## Protocol references

- Weathercloud API v1.0: <https://weathercloud.net/en/api>. Explicit examples are `rainrate=200` → 20 mm/h, `rain=350` → 35 mm daily total, `solarrad=10500` → 1050 W/m². It documents a 10-minute interval (1 minute for Pro/Premium).
- Wunderground PWS Upload Protocol: <https://support.weather.com/s/article/PWS-Upload-Protocol>. `rainin` is inches over the past 60 minutes; `dailyrainin` is inches for the local day; documented solar is lowercase `solarradiation` in W/m².
- HA 2026.10.2 sensor implementation: <https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/sensor/__init__.py>. Tests execute the pinned installed implementation rather than reproducing its converters manually.
