"""Live smoke test of the AUX HOME client against the real cloud.

Run from the repository root with the project venv:

    .venv/bin/python scripts/live_check_aux_home.py [--control-test]

Prompts for the AUX HOME email (exact case) and password; nothing is stored.
Output hides device IDs. ``--control-test`` re-sends the current fan speed, a
no-op write that proves the control path works.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.aux_cloud.api.models import AuxCredentials
from custom_components.aux_cloud.dna.home import AuxHomeClient


async def main(control_test: bool) -> None:
    email = input("AUX HOME email (exact case): ").strip()
    password = getpass.getpass("Password: ")
    async with aiohttp.ClientSession() as session:
        client = AuxHomeClient("eu", session)
        await client.login(AuxCredentials.exact_email(email, password))
        print("login: ok")
        snapshot = await client.scan_devices()
        print(f"devices: {len(snapshot.devices)} (complete={snapshot.complete})")
        for index, device in enumerate(snapshot.devices, start=1):
            profile = device["profile"]
            print(
                f"  #{index} product={device['productId']!r} "
                f"profile={profile.model_name!r} state={device.get('state')}"
            )
            print(f"     params={device.get('params')}")
        if control_test:
            await _control_test(client, snapshot.devices)


async def _control_test(client: AuxHomeClient, devices: tuple) -> None:
    for device in devices:
        fan = device.get("params", {}).get("wind_speed")
        if device["profile"].query_tags and fan is not None:
            result = await client.set_device_params(device, {"wind_speed": fan})
            print(f"control no-op (wind_speed={fan}): ok -> {result}")
            return
    print("control test skipped: no online supported device")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-test", action="store_true")
    asyncio.run(main(parser.parse_args().control_test))
