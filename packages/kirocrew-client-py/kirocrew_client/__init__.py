"""kirocrew-client — async Python client for the KiroCrew Gateway.

Usage::

    from kirocrew_client import KiroCrewClient

    async with KiroCrewClient(app_name="my-app") as mc:
        ok = await mc.ping()
        status = await mc.get_status()
        await mc.send_message("slot-1", "hello")
"""
from kirocrew_client.client import KiroCrewClient
from kirocrew_client.errors import KiroCrewError, ErrorCode

__all__ = ["KiroCrewClient", "KiroCrewError", "ErrorCode"]
