# AGENTS.md

Home Assistant custom integration `aux_cloud` (HACS). It talks to the AUX cloud
(BroadLink-based, reverse engineered from the AC Freedom app).

**Project goal:** make it work with the **AUX HOME** app/servers, and primarily
get the owner's **dehumidifier** working in Home Assistant (today only AC and
heat-pump profiles exist).

## Layout (`custom_components/aux_cloud/`)
- `__init__.py`, `config_flow.py`, `const.py`: setup, login/reauth/reconfigure.
- `coordinator.py`: websocket push first, HTTP polling fallback.
- `state.py`, `entity.py`: param merging, optimistic updates, base entity.
- `devices.py`: product profiles that decide which entities exist (add a
  dehumidifier profile here). `device_metadata.py`, `identifiers.py` support it.
- Platforms: `climate`, `water_heater`, `sensor`, `switch`, `select`, `number`
  (a dehumidifier likely needs a `humidifier` platform plus sensors).
- `dna/`: cloud protocol (`client`, `http`, `websocket`, `codec`, `inventory`).
- `api/`: models and error types.
- `strings.json`, `translations/{en,pl,el}.json`, `icons.json`, `brand/`.
- `tests/`: pytest, mirrors modules.

## Commands
Python 3.14. Setup: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.test.txt`
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
