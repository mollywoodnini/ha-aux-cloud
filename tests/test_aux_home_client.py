"""AUX HOME cloud client tests (login crypto, polling, commands)."""

import asyncio
import base64
import contextlib

import aiohttp
import pytest
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import unpad

import custom_components.aux_cloud.dna.home as home_module
from custom_components.aux_cloud.api import (
    AuxAuthError,
    AuxDeviceError,
    AuxNetworkError,
    AuxServerError,
    AuxSessionExpired,
    AuxUnknownApiError,
)
from custom_components.aux_cloud.api.models import AuxCredentials
from custom_components.aux_cloud.dna import create_cloud_client
from custom_components.aux_cloud.dna.home import (
    ACCOUNT_KEY,
    AuxHomeClient,
    anonymous_bearer,
    encrypt_account,
    encrypt_password,
    raise_for_home_response,
    status_to_params,
)

DEVICE_ID = "d50001000700000000aa00"
STATUS = {
    "mode": 3,
    "healthy": 0,
    "environment_humidity": 55,
    "fault_codes": {"fault_codes": []},
    "humidity": 50,
    "air_filter": {"runtime_percentage": 31, "needs_cleaning": 0},
    "wind_speed": 5,
    "support_function": {"mode": {"eco": 0}},
    "child_lock": 0,
    "pump": 0,
    "onoff": 1,
}
DEVICE_LIST = {
    "code": 200,
    "data": [
        {
            "deviceId": DEVICE_ID,
            "productKey": "00010007",
            "mac": "AABBCCDDEEFF",
            "alias": "Dehumidifier",
            "online": True,
        },
        {"deviceId": "other1", "productKey": "99999999", "online": True},
    ],
}
KEY = RSA.generate(1024)  # noqa: S505 - matches the cloud's key size
PUBLIC_KEY = base64.b64encode(KEY.publickey().export_key("DER")).decode()


class FakeResponse:
    """Minimal aiohttp response."""

    headers: dict[str, str] = {}

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    """Route requests to canned payloads and record every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kwargs):
        path = url.split("/app/", 1)[1]
        self.calls.append((method, path, kwargs))
        route = self.routes[path]
        if callable(route):
            return FakeResponse(route(kwargs))
        return FakeResponse(route.pop(0) if isinstance(route, list) else route)

    def paths(self):
        return [path for _method, path, _kwargs in self.calls]


def _login_ok():
    return {
        "code": 200,
        "data": {
            "appUser": {"uid": "user-1"},
            "token": {"token": "session-token"},
        },
    }


def _query_ok(**overrides):
    data = {"online": True, "sn": 1, "mcuData": "00", "status": dict(STATUS)}
    data.update(overrides)
    return {"code": 200, "data": data}


def _routes(**extra):
    routes = {
        "auth/getPubkey": {"code": 200, "data": PUBLIC_KEY},
        "auth/login/pwd": _login_ok(),
        "user_device": DEVICE_LIST,
        "device/through/commonQuery": _query_ok(),
        "device/through/commonControl": {
            "code": 200,
            "data": {"online": True, "success": True},
        },
    }
    routes.update(extra)
    return routes


def _client(**extra):
    session = FakeSession(_routes(**extra))
    return AuxHomeClient("eu", session), session


async def _logged_in(**extra):
    client, session = _client(**extra)
    await client.login(AuxCredentials.exact_email("Mixed.Case@Example.com", "p@ss%1"))
    return client, session


def test_account_and_password_encryption_round_trip():
    """Account uses the fixed AES key; the password is RSA PKCS#1 v1.5."""
    account = base64.b64decode(encrypt_account("Mixed.Case@Example.com"))
    plain = unpad(AES.new(ACCOUNT_KEY, AES.MODE_ECB).decrypt(account), 16)
    assert plain == b"Mixed.Case@Example.com"
    assert encrypt_account("mixed.case@example.com") != encrypt_account(
        "Mixed.Case@Example.com"
    )

    encrypted = base64.b64decode(encrypt_password(PUBLIC_KEY, "p@ss%1"))
    assert PKCS1_v1_5.new(KEY).decrypt(encrypted, b"") == b"p@ss%1"


def test_anonymous_bearer_has_the_app_shape():
    """Pre-login tokens are base64('<32 hex>@<unix time>')."""
    scheme, _, token = anonymous_bearer().partition(" ")
    uid, _, issued = base64.b64decode(token).decode().partition("@")
    assert scheme == "bearer"
    assert len(uid) == 32
    assert issued.isdigit()


def test_status_is_flattened_to_scalar_params():
    """Nested air-filter and fault objects become scalar params."""
    assert status_to_params(STATUS) == {
        "onoff": 1,
        "mode": 3,
        "wind_speed": 5,
        "humidity": 50,
        "child_lock": 0,
        "environment_humidity": 55,
        "filter_runtime_percentage": 31,
        "filter_needs_cleaning": 0,
        "fault_codes": [],
    }
    assert status_to_params({"onoff": True, "humidity": "x", "air_filter": 1}) == {}
    faults = {"fault_codes": {"fault_codes": [1, "e2"]}}
    assert status_to_params(faults) == {"fault_codes": ["1", "e2"]}


def test_response_codes_map_to_typed_errors():
    """Known AUX HOME codes map to the shared error types."""
    raise_for_home_response({"code": 200}, endpoint="x")
    with pytest.raises(AuxAuthError):
        raise_for_home_response({"code": 60009, "message": "no"}, endpoint="x")
    with pytest.raises(AuxSessionExpired):
        raise_for_home_response({"code": 60106}, endpoint="x")
    with pytest.raises(AuxUnknownApiError):
        raise_for_home_response({"code": 12345}, endpoint="x")
    with pytest.raises(AuxServerError):
        raise_for_home_response({"message": "no code"}, endpoint="x")


async def test_login_sends_exact_case_account_and_raw_password():
    """The request matches the app: AES account, RSA password, echoed key."""
    client, session = await _logged_in()

    assert client.is_logged_in()
    assert client.user_id == "user-1"
    method, path, kwargs = session.calls[1]
    assert (method, path) == ("POST", "auth/login/pwd")
    body = kwargs["json"]
    assert list(body) == ["password", "account", "ts", "publicKeyBase64"]
    assert body["publicKeyBase64"] == PUBLIC_KEY
    account = base64.b64decode(body["account"])
    assert unpad(AES.new(ACCOUNT_KEY, AES.MODE_ECB).decrypt(account), 16) == (
        b"Mixed.Case@Example.com"
    )
    password = base64.b64decode(body["password"])
    assert PKCS1_v1_5.new(KEY).decrypt(password, b"") == b"p@ss%1"
    assert kwargs["headers"]["Authorization"].startswith("bearer ")


async def test_login_retries_once_when_the_public_key_expired():
    """An expired one-time key is replaced exactly once."""
    expired = {"code": 64033, "message": "key expired"}
    client, session = _client(**{"auth/login/pwd": [expired, _login_ok()]})
    await client.login(AuxCredentials.exact_email("a@b.c", "pw"))
    assert session.paths() == [
        "auth/getPubkey",
        "auth/login/pwd",
        "auth/getPubkey",
        "auth/login/pwd",
    ]

    client, _ = _client(**{"auth/login/pwd": [expired, expired]})
    with pytest.raises(AuxUnknownApiError):
        await client.login(AuxCredentials.exact_email("a@b.c", "pw"))


async def test_wrong_password_and_bad_responses_surface():
    """Credential rejection and malformed logins raise typed errors."""
    client, _ = _client(**{"auth/login/pwd": {"code": 60009, "message": "wrong"}})
    with pytest.raises(AuxAuthError):
        await client.login(AuxCredentials.exact_email("a@b.c", "pw"))
    assert not client.is_logged_in()

    client, _ = _client(**{"auth/login/pwd": {"code": 200, "data": {}}})
    with pytest.raises(AuxServerError):
        await client.login(AuxCredentials.exact_email("a@b.c", "pw"))

    client, _ = _client(**{"auth/getPubkey": {"code": 200, "data": None}})
    with pytest.raises(AuxServerError):
        await client.login(AuxCredentials.exact_email("a@b.c", "pw"))


async def test_scan_devices_queries_supported_products_only():
    """The profile decides what is queried; unknown products stay param-less."""
    client, session = await _logged_in()

    snapshot = await client.scan_devices()

    assert snapshot.complete
    dehumidifier, unknown = snapshot.devices
    assert dehumidifier["endpointId"] == DEVICE_ID
    assert dehumidifier["productId"] == "00010007"
    assert dehumidifier["state"] == 1
    assert dehumidifier["params"]["humidity"] == 50
    assert dehumidifier["mac"] == "AABBCCDDEEFF"
    assert unknown["params"] == {}
    queries = [c for c in session.calls if c[1] == "device/through/commonQuery"]
    assert len(queries) == 1
    body = queries[0][2]["json"]
    assert body["deviceId"] == DEVICE_ID
    assert "support_function" in body["tags"]
    assert queries[0][2]["headers"]["Authorization"] == "bearer session-token"


async def test_offline_and_failed_queries_are_reported():
    """Offline devices have no params; failed queries make the scan incomplete."""
    client, _ = await _logged_in(
        **{"device/through/commonQuery": _query_ok(online=False, status=None)}
    )
    snapshot = await client.scan_devices()
    assert snapshot.complete
    assert snapshot.devices[0]["state"] == 0
    assert snapshot.devices[0]["params"] == {}

    client, _ = await _logged_in(**{"device/through/commonQuery": _query_ok(status={})})
    snapshot = await client.scan_devices()
    assert not snapshot.complete


async def test_expired_session_recovers_once_with_stored_credentials():
    """code 60106 triggers one silent re-login and a retry."""
    expired = {"code": 60106, "message": "expired"}
    client, session = await _logged_in(**{"user_device": [expired, DEVICE_LIST]})

    snapshot = await client.scan_devices()

    assert len(snapshot.devices) == 2
    assert session.paths().count("auth/login/pwd") == 2

    client, _ = await _logged_in(**{"user_device": [expired, expired]})
    with pytest.raises(AuxSessionExpired):
        await client.scan_devices()


async def test_requests_need_a_login():
    """Unauthenticated device calls fail fast."""
    client, _ = _client()
    with pytest.raises(AuxSessionExpired):
        await client.scan_devices()


async def test_set_device_params_sends_tags_and_checks_the_result():
    """Commands go to commonControl and offline devices raise."""
    client, session = await _logged_in()
    device = {"endpointId": DEVICE_ID}

    result = await client.set_device_params(device, {"mode": 5, "wind_speed": 5})

    assert result == {"mode": 5, "wind_speed": 5}
    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "device/through/commonControl")
    assert kwargs["json"] == {
        "deviceId": DEVICE_ID,
        "tags": {"mode": 5, "wind_speed": 5},
        "dst": 1,
    }

    offline = {"code": 200, "data": {"online": False, "success": False}}
    client, _ = await _logged_in(**{"device/through/commonControl": offline})
    with pytest.raises(AuxDeviceError):
        await client.set_device_params(device, {"onoff": 1})

    failed = {"code": 200, "data": {"online": True, "success": False}}
    client, _ = await _logged_in(**{"device/through/commonControl": failed})
    with pytest.raises(AuxDeviceError):
        await client.set_device_params(device, {"onoff": 1})


async def test_network_errors_are_typed():
    """Transport failures become AuxNetworkError."""

    def boom(_kwargs):
        raise aiohttp.ClientError

    client, _ = await _logged_in(**{"user_device": boom})
    with pytest.raises(AuxNetworkError):
        await client.scan_devices()


async def test_realtime_polling_publishes_updates_and_tracks_health(monkeypatch):
    """The poller feeds updates like a push channel and reports degradation."""
    monkeypatch.setattr(home_module, "POLL_INTERVAL_SECONDS", 0)
    client, session = await _logged_in()
    snapshot = await client.scan_devices()
    updates, health = [], []
    first = asyncio.Event()

    def listener(batch):
        updates.append(batch)
        first.set()

    task = asyncio.create_task(
        client.run_realtime(
            devices=list(snapshot.devices),
            listener=listener,
            connection_listener=health.append,
        )
    )
    await asyncio.wait_for(first.wait(), 2)
    update = updates[0][0]
    assert update.endpoint_id == DEVICE_ID
    assert update.available is True
    assert update.params["environment_humidity"] == 55

    def boom(_kwargs):
        raise aiohttp.ClientError

    session.routes["device/through/commonQuery"] = boom
    for _ in range(100):
        if False in health:
            break
        await asyncio.sleep(0)
        await asyncio.sleep(0.001)
    assert health[0] is True
    assert False in health
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_update_realtime_devices_and_close_are_safe():
    """Membership updates and close do not need network access."""
    client, _ = _client()
    await client.update_realtime_devices([])
    await client.close()


def test_factory_selects_the_backend():
    """Entries choose the cloud client by backend key."""
    assert isinstance(create_cloud_client("aux_home", "eu", None), AuxHomeClient)
    assert not isinstance(create_cloud_client("dna", "eu", None), AuxHomeClient)
