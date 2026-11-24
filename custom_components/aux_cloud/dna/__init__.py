"""AUX cloud implementations (BroadLink DNA and AUX HOME)."""

import aiohttp

from ..api import AuxCloudClient
from ..const import BACKEND_AUX_HOME
from .client import DnaClient
from .home import HOME_REGIONS, AuxHomeClient


def create_cloud_client(
    backend: str, region: str, session: aiohttp.ClientSession
) -> AuxCloudClient:
    """Return the cloud client for a config-entry backend."""
    if backend == BACKEND_AUX_HOME:
        return AuxHomeClient(region=region, session=session)
    return DnaClient(region=region, session=session)


__all__ = ["HOME_REGIONS", "AuxHomeClient", "DnaClient", "create_cloud_client"]
