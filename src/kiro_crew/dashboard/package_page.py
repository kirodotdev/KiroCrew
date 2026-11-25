"""The applied page of an unpinned dashboard package, for readers that hold no request.

A crewmate whose instance record predates pinned packages draws the package bound to it,
at the newest version a person cut. The page route checks those bytes inline in
``member_dashboard._serve_package``; the agent panel's snapshot and the phone widget
resolver ask :func:`applied_package_page`, so the two of them read one rule.
"""

from __future__ import annotations


class PackagePageRefused(Exception):
    """An unpinned package has no page a reader may be shown; :attr:`reason` says why.

    ``not_a_package`` (another kind, or a linked file whose bytes skip the write gate),
    ``not_applied`` (no version a person cut) or ``replaced`` (the record changed
    between the approval read and the versioned read).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def applied_package_page(package_slug: str) -> tuple[int, str]:
    """``(version, content)`` of the version of *package_slug* a person cut, or raise.

    The page route's checks (``member_dashboard._serve_package``) for a reader with no
    request: the record is a stored ``kind="dashboard"`` artifact that owns its bytes,
    its applied version is ``member_dashboard.applied_package_version``, and that
    version is read from the same record (``member_dashboard._record_identity``). A
    failed check raises :class:`PackagePageRefused`; a store error propagates.
    Blocking IO.
    """
    from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND
    from kiro_crew.artifacts import get_default_store
    from kiro_crew.dashboard.handlers import member_dashboard as md

    store = get_default_store()
    loaded = store.get(package_slug)
    if str(getattr(loaded, "kind", "")) != DASHBOARD_KIND or (
        str(getattr(loaded, "source_path", "") or "")
        and not bool(getattr(loaded, "source_copy_only", False))
    ):
        raise PackagePageRefused("not_a_package")
    version = md.applied_package_version(loaded)
    if version <= 0:
        raise PackagePageRefused("not_applied")
    applied = store.get(package_slug, version=version)
    if md._record_identity(applied) != md._record_identity(loaded):
        raise PackagePageRefused("replaced")
    return version, applied.content or ""
