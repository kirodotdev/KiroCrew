"""The report sections of ``kirocrew doctor``, one module per family.

:mod:`kiro_crew.cli_doctor` is the command's facade and orchestrator: it prints the
sections in a fixed order, owns the rows repository gates pin to that file, and
renders the verdict and exit code. The families here hold the rest:

* :mod:`~kiro_crew.doctor_checks.render` -- the escaping, indent and wrapping
  helpers the sections share
* :mod:`~kiro_crew.doctor_checks.agents` -- Crew members, deprecated agent specs, the
  optional Claude Code backend row and the per-harness sign-in rows
* :mod:`~kiro_crew.doctor_checks.mcp` -- strict-identity routing, the MCP gateway
  daemon, and each harness's MCP projection
* :mod:`~kiro_crew.doctor_checks.confinement` -- the sandbox backend verdict and the
  shapes that make the launcher refuse a spawn
* :mod:`~kiro_crew.doctor_checks.access` -- session signing, hook auto-approve and the
  credential posture an agent sees
* :mod:`~kiro_crew.doctor_checks.install` -- the project directory, data home, launcher,
  deployed skill content, source checkout and Python runtime
* :mod:`~kiro_crew.doctor_checks.services` -- the service definition, its environment,
  the pod session bus, the dashboard's bind and auth, and whether the gateway answers
* :mod:`~kiro_crew.doctor_checks.resources` -- memory pressure, tmpfs headroom,
  installer residue and the agents directory
* :mod:`~kiro_crew.doctor_checks.workload` -- cron health, the task queue, overload
  bounds and loop-stall history
* :mod:`~kiro_crew.doctor_checks.channels` -- Slack, Discord, WhatsApp and every other
  channel
* :mod:`~kiro_crew.doctor_checks.features` -- vector memory and speech-to-text

The orchestrator imports these when a health report runs (``--bundle`` prints no
report and imports none of them), so ``import kiro_crew.cli`` never pays for them.
"""
