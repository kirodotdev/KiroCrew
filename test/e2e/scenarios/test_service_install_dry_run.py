"""Scenario: the host service definition renders correctly inside a pod's environment.

The flow being protected: someone runs ``kirocrew service install`` from a shell
that a pod has been driving. ``service install`` bakes an environment into a
systemd unit or a launchd plist, because a service starts with a minimal,
non-login environment. Two halves of that capture must behave differently, and
this pins both. ``KIROCREW_PORT`` is captured on purpose, since it is the only
input the dashboard port reads. The data home is not: it comes from the account
the service runs as, so a pod's throwaway home must never reach it, because
``pod down`` deletes that directory.

Print-only. ``service install`` has no ``--dry-run``, so this drives the pure
renderer the installer itself calls, in a child process carrying the pod's
environment. Nothing is written and no service manager is touched.

Backend dispatch lives in the fixture module, so this file has no platform
branching -- which is what makes Windows a matrix add rather than an edit here.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.timeout(300)


def test_host_service_render_captures_the_port_but_not_the_pod_home(
    pod, render_service_definition
) -> None:
    # Everything a pod exports about itself, in scope for the render.
    text = render_service_definition(
        {
            "KIROCREW_HOME": str(pod.home),
            "KIROCREW_PORT": str(pod.port),
            "KIROCREW_POD": "1",
            "KIROCREW_WORKSPACE": str(pod.home / "workspace"),
        }
    )
    assert text.strip(), "the service renderer printed nothing"

    # The definition still describes a HOST service.
    assert "KIROCREW_SERVICE_MANAGED" in text, (
        f"the render carries no managed marker, so it lost its baked "
        f"environment.\nrendered:\n{text}"
    )
    assert "gateway" in text, f"the render names no gateway command.\nrendered:\n{text}"

    # The port IS captured from the installer's environment, by documented design:
    # KIROCREW_PORT is the only input DASHBOARD_PORT reads, so a service that
    # could not carry it would be stuck on the default 5476. Asserted in that
    # direction rather than against a leak, so this scenario pins the contract
    # instead of contradicting it.
    assert f"KIROCREW_PORT={pod.port}" in text or f">{pod.port}<" in text, (
        f"the render dropped KIROCREW_PORT={pod.port} from the installer's "
        f"environment, so an installed service would fall back to the default "
        f"port.\nrendered:\n{text}"
    )

    # The HOME is the opposite case and the sharp one: it is resolved from the
    # ACCOUNT the service runs as, never from KIROCREW_HOME, so a pod's throwaway
    # home must not reach it. `pod down` deletes that directory, and a service
    # installed with it baked in would start against a path that is not there.
    assert str(pod.home) not in text, (
        f"the pod's throwaway home {pod.home} leaked into the host service "
        f"definition.\nrendered:\n{text}"
    )
    assert str(pod.home / "workspace") not in text, (
        f"the pod's throwaway workspace leaked into the host service "
        f"definition.\nrendered:\n{text}"
    )
    assert "KIROCREW_POD" not in text, (
        f"the pod-identity marker leaked into the host service definition, which "
        f"would make an installed gateway declare itself ephemeral."
        f"\nrendered:\n{text}"
    )


def test_render_names_the_home_the_service_would_run_as(pod, render_service_definition) -> None:
    """The render must name the account home the installer resolved, absolutely.

    Deliberately asserted against the renderer's OWN resolver rather than
    against ``$HOME``: the two backends resolve it differently on purpose (the
    systemd render reads the passwd entry so ``sudo -H`` cannot bake ``/root``
    in, the launchd render uses ``Path.home()``), and pinning one would make
    this scenario a platform test. What must hold on both is that the home in
    the text is the one that backend resolves, spelled absolutely.
    """
    import subprocess
    import sys

    text = render_service_definition({})
    resolved = subprocess.run(
        [
            sys.executable,
            "-c",
            "from kiro_crew.service.common import Platform, current_platform\n"
            "plat = current_platform()\n"
            "if plat is Platform.SYSTEMD:\n"
            "    from kiro_crew.service import linux\n"
            "    print(linux._home_for_user(linux._current_user()))\n"
            "else:\n"
            "    from pathlib import Path\n"
            "    print(Path.home())\n",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    home = resolved.stdout.strip()
    assert home.startswith("/"), f"resolver gave no absolute home: {resolved!r}"
    assert home in text, (
        f"the render does not name the home its own resolver reports "
        f"({home}).\nrendered:\n{text}"
    )
