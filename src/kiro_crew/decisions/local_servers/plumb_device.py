"""Which device Plumb-4B's launcher loads the model on.

Kept apart from ``plumb_cpu.py`` so the gateway's tests can import it: the
launcher itself imports torch and starts a server at import time. The runtime
copies this file beside the launcher, which imports it from there.
"""


def resolve_device(requested: str, torch: object) -> str:
    """``cuda`` only when the gateway asked for it and *torch* can use it."""
    if requested != "cuda":
        return "cpu"
    try:
        if torch.cuda.is_available():  # type: ignore[attr-defined]
            return "cuda"
    except Exception:
        return "cpu"
    return "cpu"
