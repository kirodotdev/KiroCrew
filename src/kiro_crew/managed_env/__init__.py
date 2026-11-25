"""Kiro managed environments for Remote Crew, behind the ``crew_client`` feature preview.

A managed environment is a persistent cloud machine the Kiro web service creates for
the signed-in user. The lane creates one, installs Crew on it, and adds it to Remote
Crew; it then lists, stops, resumes and deletes the ones it made.

* :mod:`.client`, :mod:`.guard`, :mod:`.settings`: the service client, its memory of
  what it may touch, and the ``instances.managed_env_*`` keys;
* :mod:`.engine`: the core's launch engine for the lane, over :mod:`.remote`,
  :mod:`.install` and :mod:`.machine` (the seam to machine mode);
* :mod:`.routes`: the dashboard's ``/api/managed-environments/*`` routes;
* :mod:`.provider`: the platform-seam pieces that offer the lane in the stock build.
"""

#: The Remote Crew provisioner id the lane's rows are stamped with.
PROVISIONER_ID = "kiro_managed_env"
PROVISIONER_KIND = "kiro_managed_env"
