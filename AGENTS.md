# AGENTS.md

Home Assistant custom integration `aux_cloud` (HACS). It talks to the AUX cloud
(BroadLink-based, reverse engineered from the AC Freedom app).

**Project goal:** make it work with the **AUX HOME** app/servers, and primarily
get the owner's **dehumidifier** working in Home Assistant. Status: client,
profile and entities are implemented and unit tested; they still need a live
check against the real cloud (`scripts/live_check_aux_home.py`).

## Layout (`custom_components/aux_cloud/`)
- `__init__.py`, `config_flow.py`, `const.py`: setup, login/reauth/reconfigure.
- `coordinator.py`: websocket push first, HTTP polling fallback.
- `state.py`, `entity.py`: param merging, optimistic updates, base entity.
- `devices.py`: product profiles that decide which entities exist (add a
  dehumidifier profile here). `device_metadata.py`, `identifiers.py` support it.
- Platforms: `climate`, `water_heater`, `sensor`, `switch`, `select`, `number`,
  `humidifier` (dehumidifier).
- `dna/`: cloud protocol. BroadLink DNA (AC Freedom): `client`, `http`,
  `websocket`, `codec`, `inventory`. AUX HOME: `home.py` (`AuxHomeClient`).
  `dna/__init__.py:create_cloud_client` picks the client from the entry's
  `backend` (`dna` default, `aux_home`).
- `api/`: models and error types.
- `strings.json`, `translations/{en,pl,el}.json`, `icons.json`, `brand/`.
- `tests/`: pytest, mirrors modules.

## AUX HOME protocol (reverse engineered, EU only)
Base `https://eu-smthome-api.aux-global.com/app/`, JSON, always HTTP 200 with
`{"code":200,"message","data"}`. Headers: `os: 2`, `aid: 1`, `country: DEU`,
`Authorization: bearer <token>`. Codes: 60009 wrong password, 60106 session
expired, 64033 public key expired.
- Login: `GET auth/getPubkey` (RSA-1024, new key per call, works anonymously, send
  any `bearer base64(<32hex>@<unix>)`), then `POST auth/login/pwd` with
  `{password: RSA-PKCS1v1.5(raw password), account: AES-128-ECB/PKCS7(email, key
  "4083aux63e3444a2") base64, ts: ms, publicKeyBase64: the key used}`. The email
  is case sensitive: never lower-case it. Token = `data.token.token`, user
  `data.appUser.uid`; tokens live at least weeks.
- Devices: `GET user_device?getStatus=1` (`deviceId`, `productKey`, `alias`).
- State: `POST device/through/commonQuery {deviceId, tags[], dst:1}`.
  Control: `POST device/through/commonControl {deviceId, tags{}, dst:1}`
  (returns only `online`/`success`, not the new state).
- Dehumidifier `00010007`: `onoff`, `mode` 1 continuous / 3 manual / 5 drying
  (app sends `wind_speed` 5 with mode 5), `wind_speed` 1/3/5, `humidity` target
  30-80 step 5, `environment_humidity`, `child_lock`, nested `air_filter` and
  `fault_codes`. Pump/healthy exist in the protocol but this unit lacks them.
- Unverified: websocket/push, other regions, shared devices, session lifetime.

## Commands
Python 3.14. Setup: `python3.14 -m venv .venv && . .venv/bin/activate && pip install -r requirements.test.txt`
- `ruff format --check . && ruff check .`
- `mypy custom_components/aux_cloud` (strict)
- `pytest -q` (asyncio auto mode, coverage must stay >= 60%)
CI runs all of these against min HA 2026.4.* and latest. Run them before finishing.

## Conventions
- ruff line length 88, max McCabe complexity 10; mypy strict; fully typed code.
- Use `from __future__ import annotations`.
- Profiles decide entities; live cloud keys must never add/remove entities.
- Never log credentials/tokens; keep diagnostics redacted. Verify TLS.
- Serialize commands; keep websocket/HTTP behaviour in `dna/`, not in platforms.
- New user-facing text goes in `strings.json` and all of en/pl/el translations.
- Add tests for every new profile/entity; update README when features change.
- Bump `manifest.json` version only when asked.

## Adding a new device type (e.g. dehumidifier)
1. Capture real device params (diagnostics output / cloud `getdevicestate`) and
   record the param names and value meanings; do not guess protocol keys.
2. Add constants + profile in `devices.py`, keyed by product id.
3. Add/extend platform entities driven by the profile.
4. Add translations, icons, tests with fixture payloads, README entry.
5. Check AUX HOME vs AC Freedom differences (region servers, app ids, login) in `dna/`.

## Safety
- Do not commit, push, or publish unless asked. Never commit real account data
  or device IDs; sanitize fixtures.
