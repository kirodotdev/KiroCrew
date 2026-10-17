"""kirocrew-client — async Python client for the KiroCrew Gateway.

Usage::

    from kirocrew_client import KiroCrewClient

    async with KiroCrewClient(app_name="my-app") as mc:
        ok = await mc.ping()
        status = await mc.get_status()
        await mc.send_message("slot-1", "hello")
"""
from kirocrew_client.client import GATEWAY_CONFIG_KEYS, KiroCrewClient
from kirocrew_client.errors import KiroCrewError, ErrorCode
from kirocrew_client.ws_client import WsClient, WsEvent

__all__ = [
    "GATEWAY_CONFIG_KEYS",
    "KiroCrewClient",
    "KiroCrewError",
    "ErrorCode",
    "WsClient",
    "WsEvent",
]
