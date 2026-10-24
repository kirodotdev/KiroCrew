# KiroCrew 0.9 adaptive integration

This adapter was source-audited against the KiroCrew 0.9.0 Insider family and registers Kiro BYO as
a multiplexed `HarnessAdapter` served by Crew's shared `AcpRuntime`.

It intentionally does **not** patch `acp/client.py`; 0.9 owns busy-session queueing,
session reuse/reload and shared-session orchestration in `AcpRuntime`.

The transform is fail-closed: every required semantic seam must still be recognizable
before any file is installed.
