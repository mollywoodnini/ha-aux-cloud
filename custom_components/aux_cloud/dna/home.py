"""AUX HOME (aux-global.com) cloud client.

AUX HOME is a REST/JSON backend that is unrelated to the BroadLink DNA cloud used
by AC Freedom. Login encrypts the account with a fixed AES key and the password
with a one-time RSA public key served by the cloud. Device state is polled with
``device/through/commonQuery`` and changed with ``commonControl``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any, cast

import aiohttp
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

from ..api.errors import (
    AuxApiError,
    AuxAuthError,
    AuxDeviceError,
    AuxNetworkError,
    AuxServerError,
    AuxSessionExpired,
    AuxUnknownApiError,
    raise_for_http_status,
)
from ..api.models import AuxCredentials, AuxDevice, DeviceUpdate, InventorySnapshot
from ..devices import (
    DH_AMBIENT_HUMIDITY,
    DH_CHILD_LOCK,
    DH_FAN_SPEED,
    DH_FAULT_CODES,
    DH_FILTER_NEEDS_CLEANING,
    DH_FILTER_RUNTIME,
    DH_MODE,
    DH_POWER,
    DH_TARGET_HUMIDITY,
    get_product_profile,
)

_LOGGER = logging.getLogger(__name__)

HOME_API_URLS = {"eu": "https://eu-smthome-api.aux-global.com/app"}
HOME_REGIONS = tuple(HOME_API_URLS)

# AES-128-ECB key the app embeds for the ``account`` login field.
ACCOUNT_KEY = b"4083aux63e3444a2"

CODE_OK = 200
CODE_PASSWORD_ERROR = 60009
CODE_SESSION_EXPIRED = 60106
CODE_PUBLIC_KEY_EXPIRED = 64033

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
POLL_INTERVAL_SECONDS = 30
FAILURES_BEFORE_DEGRADED = 3

_APP_HEADERS = {
    "os": "2",
    "aid": "1",
    "country": "DEU",
    "User-Agent": "AUXAC/2.5.2 (iPhone; iOS 26.7.2; Scale/3.00)",
    "Accept-Language": "en",
}
_INT_STATUS_KEYS = (
    DH_POWER,
    DH_MODE,
    DH_FAN_SPEED,
    DH_TARGET_HUMIDITY,
    DH_CHILD_LOCK,
    DH_AMBIENT_HUMIDITY,
)


def encrypt_account(email: str) -> str:
    """Encrypt the account name exactly as the AUX HOME app does."""
    cipher = AES.new(ACCOUNT_KEY, AES.MODE_ECB)
    return base64.b64encode(
        cipher.encrypt(pad(email.encode(), AES.block_size))
    ).decode()


def encrypt_password(public_key_b64: str, password: str) -> str:
    """Encrypt the raw password with the cloud-issued RSA public key."""
    key = RSA.import_key(base64.b64decode(public_key_b64))
    return base64.b64encode(PKCS1_v1_5.new(key).encrypt(password.encode())).decode()


def anonymous_bearer() -> str:
    """Return a pre-login bearer value in the shape the app sends."""
    raw = f"{secrets.token_hex(16)}@{int(time.time())}"
    return "bearer " + base64.b64encode(raw.encode()).decode()


def status_to_params(status: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a commonQuery ``status`` object into scalar device params."""
    params: dict[str, Any] = {
        key: value
        for key in _INT_STATUS_KEYS
        if isinstance(value := status.get(key), int) and not isinstance(value, bool)
    }
    air_filter = status.get("air_filter")
    if isinstance(air_filter, Mapping):
        if isinstance(runtime := air_filter.get("runtime_percentage"), int):
            params[DH_FILTER_RUNTIME] = runtime
        if isinstance(cleaning := air_filter.get("needs_cleaning"), int):
            params[DH_FILTER_NEEDS_CLEANING] = cleaning
    faults = status.get("fault_codes")
    if isinstance(faults, Mapping) and isinstance(
        codes := faults.get("fault_codes"), list
    ):
        params[DH_FAULT_CODES] = [str(code) for code in codes]
    return params


def raise_for_home_response(payload: Mapping[str, Any], *, endpoint: str) -> None:
    """Raise a typed error when an AUX HOME response is not a success."""
    code = payload.get("code")
    if code == CODE_OK:
        return
    if not isinstance(code, int):
        raise AuxServerError(
            "AUX HOME returned an unexpected response", endpoint=endpoint
        )
    message = str(payload.get("message") or "")
    detail = f"AUX HOME error {code}: {message}" if message else None
    if code == CODE_PASSWORD_ERROR:
        raise AuxAuthError(detail, code=code, endpoint=endpoint)
    if code == CODE_SESSION_EXPIRED:
        raise AuxSessionExpired(detail, code=code, endpoint=endpoint)
    raise AuxUnknownApiError(detail, code=code, endpoint=endpoint)


class AuxHomeClient:
    """AUX HOME implementation of the AUX cloud client contract."""

    def __init__(self, region: str, session: aiohttp.ClientSession) -> None:
        """Initialize the client; unknown regions fall back to Europe."""
        self._base_url = HOME_API_URLS.get(region, HOME_API_URLS["eu"])
        self._session = session
        self._token: str | None = None
        self._user_id: str | None = None
        self._credentials: AuxCredentials | None = None
        self._login_lock = asyncio.Lock()
        self._devices: list[AuxDevice] = []

    @property
    def user_id(self) -> str | None:
        """Return the cloud user ID."""
        return self._user_id

    def is_logged_in(self) -> bool:
        """Return whether a session token is held."""
        return self._token is not None and self._user_id is not None

    async def login(self, credentials: AuxCredentials) -> None:
        """Authenticate and remember the credentials for silent re-login."""
        async with self._login_lock:
            await self._login_unlocked(credentials)

    async def _login_unlocked(self, credentials: AuxCredentials) -> None:
        """Log in while the single-flight lock is held."""
        for attempt in range(2):
            bearer = anonymous_bearer()
            public_key = (
                await self._request_once("GET", "auth/getPubkey", auth=bearer)
            ).get("data")
            if not isinstance(public_key, str):
                raise AuxServerError("AUX HOME did not return a public key")
            body = {
                "password": encrypt_password(public_key, credentials.password),
                "account": encrypt_account(credentials.username),
                "ts": str(int(time.time() * 1000)),
                "publicKeyBase64": public_key,
            }
            try:
                payload = await self._request_once(
                    "POST", "auth/login/pwd", body=body, auth=bearer
                )
            except AuxApiError as exc:
                if attempt == 0 and exc.code == CODE_PUBLIC_KEY_EXPIRED:
                    continue
                raise
            self._store_login(payload, credentials)
            return

    def _store_login(
        self, payload: Mapping[str, Any], credentials: AuxCredentials
    ) -> None:
        """Validate and keep one successful login response."""
        data = payload.get("data")
        data = data if isinstance(data, Mapping) else {}
        token = (data.get("token") or {}).get("token")
        user = data.get("appUser")
        user_id = user.get("uid") if isinstance(user, Mapping) else None
        if not isinstance(token, str) or not isinstance(user_id, str):
            raise AuxServerError("AUX HOME login response did not include an identity")
        self._token = f"bearer {token}"
        self._user_id = user_id
        self._credentials = credentials
        _LOGGER.debug("AUX HOME login successful")

    async def _recover_session(self, expired_token: str | None) -> None:
        """Log in again once with the stored credentials."""
        async with self._login_lock:
            if self._token is not None and self._token != expired_token:
                return
            self._token = None
            self._user_id = None
            if self._credentials is None:
                raise AuxAuthError(
                    "Cannot recover AUX HOME session without credentials"
                )
            await self._login_unlocked(self._credentials)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run one authenticated request with a single silent re-login."""
        for attempt in range(2):
            token = self._token
            if token is None:
                raise AuxSessionExpired("AUX HOME login is required", endpoint=path)
            try:
                return await self._request_once(
                    method, path, body=body, params=params, auth=token
                )
            except AuxSessionExpired:
                if attempt > 0:
                    raise
                await self._recover_session(token)
        raise RuntimeError("Unreachable AUX HOME request state")

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        auth: str,
    ) -> dict[str, Any]:
        """Perform one request and raise typed errors for failures."""
        try:
            async with self._session.request(
                method,
                f"{self._base_url}/{path}",
                headers={**_APP_HEADERS, "Authorization": auth},
                json=body,
                params=params,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                raise_for_http_status(
                    response.status,
                    endpoint=path,
                    retry_after=response.headers.get("Retry-After"),
                )
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise AuxNetworkError(endpoint=path) from exc
        if not isinstance(payload, dict):
            raise AuxServerError("AUX HOME returned invalid JSON", endpoint=path)
        raise_for_home_response(payload, endpoint=path)
        return cast(dict[str, Any], payload)

    async def scan_devices(self) -> InventorySnapshot:
        """Fetch the account devices and their current state."""
        payload = await self._request("GET", "user_device", params={"getStatus": "1"})
        raw_devices = payload.get("data")
        if not isinstance(raw_devices, list):
            raise AuxServerError(
                "AUX HOME device list was invalid", endpoint="user_device"
            )
        devices = [
            device
            for item in raw_devices
            if isinstance(item, Mapping) and (device := _device_from_cloud(item))
        ]
        results = await asyncio.gather(
            *(self._refresh_device(device) for device in devices),
            return_exceptions=True,
        )
        complete = True
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, (AuxAuthError, AuxSessionExpired)):
                raise result
            if isinstance(result, BaseException):
                _LOGGER.debug("AUX HOME device query failed: %s", result)
                complete = False
        self._devices = devices
        return InventorySnapshot(devices=tuple(devices), complete=complete)

    async def _refresh_device(self, device: AuxDevice) -> None:
        """Query one device in place; unknown products are left without params."""
        profile = device["profile"]
        if not profile.query_tags:
            return
        online, params = await self._query_device(
            device["endpointId"], profile.query_tags
        )
        device["state"] = 1 if online else 0
        device["params"] = params
        if online and not params:
            raise AuxServerError(
                "AUX HOME returned no device state", endpoint="commonQuery"
            )

    async def _query_device(
        self, device_id: str, tags: tuple[str, ...]
    ) -> tuple[bool, dict[str, Any]]:
        """Return whether a device is online and its flattened state."""
        payload = await self._request(
            "POST",
            "device/through/commonQuery",
            body={"deviceId": device_id, "tags": list(tags), "dst": 1},
        )
        data = payload.get("data")
        data = data if isinstance(data, Mapping) else {}
        online = bool(data.get("online"))
        status = data.get("status")
        if not online or not isinstance(status, Mapping):
            return online, {}
        return True, status_to_params(status)

    async def set_device_params(
        self, device: AuxDevice, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Send one control command; the cloud does not echo the new state."""
        payload = await self._request(
            "POST",
            "device/through/commonControl",
            body={"deviceId": device["endpointId"], "tags": dict(values), "dst": 1},
        )
        data = payload.get("data")
        data = data if isinstance(data, Mapping) else {}
        if not data.get("online"):
            raise AuxDeviceError(code=-3, endpoint="commonControl")
        if data.get("success") is False:
            raise AuxDeviceError(code=-7, endpoint="commonControl")
        return dict(values)

    async def run_realtime(
        self,
        devices: list[AuxDevice] | None = None,
        listener: Callable[[tuple[DeviceUpdate, ...]], None] | None = None,
        connection_listener: Callable[[bool], None] | None = None,
    ) -> None:
        """Poll device state and publish it like a push channel until cancelled."""
        if devices is not None:
            self._devices = list(devices)
        failures = 0
        degraded = False
        if connection_listener is not None:
            connection_listener(True)
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                updates = await self._poll_updates()
            except AuxApiError as exc:
                failures += 1
                _LOGGER.debug("AUX HOME poll failed (%s): %s", failures, exc)
                isolated_auth_failure = isinstance(exc, AuxAuthError)
                if (
                    isolated_auth_failure or failures >= FAILURES_BEFORE_DEGRADED
                ) and not degraded:
                    degraded = True
                    if connection_listener is not None:
                        connection_listener(False)
                continue
            failures = 0
            if degraded:
                degraded = False
                if connection_listener is not None:
                    connection_listener(True)
            if updates and listener is not None:
                listener(updates)

    async def _poll_updates(self) -> tuple[DeviceUpdate, ...]:
        """Query every known supported device once."""
        updates: list[DeviceUpdate] = []
        for device in self._devices:
            profile = device.get("profile")
            if profile is None or not profile.query_tags:
                continue
            online, params = await self._query_device(
                device["endpointId"], profile.query_tags
            )
            updates.append(
                DeviceUpdate(
                    endpoint_id=device["endpointId"],
                    params=params,
                    available=online,
                )
            )
        return tuple(updates)

    async def update_realtime_devices(self, devices: list[AuxDevice]) -> None:
        """Replace the devices that are polled."""
        self._devices = list(devices)

    async def close(self) -> None:
        """Nothing to close: the HTTP session is owned by Home Assistant."""


def _device_from_cloud(item: Mapping[str, Any]) -> AuxDevice | None:
    """Normalize one cloud device record."""
    device_id = item.get("deviceId")
    if not isinstance(device_id, str) or not device_id:
        return None
    product_key = item.get("productKey")
    product_id = product_key if isinstance(product_key, str) else None
    device: AuxDevice = {
        "endpointId": device_id,
        "friendlyName": str(item.get("alias") or device_id),
        "productId": product_id or "",
        "state": 1 if item.get("online") else 0,
        "params": {},
        "profile": get_product_profile(product_id),
    }
    if isinstance(mac := item.get("mac"), str) and mac:
        device["mac"] = mac
    return device
