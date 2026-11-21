"""The Kiro managed-environment lane for Remote Crew.

Behind the ``crew_client`` feature preview (default off; an LTS build reads it as off).
With it on, Remote Crew > Set up a new one offers "Managed environment": Crew creates one
of the user's Kiro managed environments, installs Crew there on the environment's machine
token, and adds it to Your crews over ssh. Every endpoint, profile and engine value comes
from the ``instances.managed_env_*`` keys, all empty by default.

Modules:

* :mod:`.settings`       the preview switch and the ``instances.managed_env_*`` values
* :mod:`.cbor`           the CBOR codec the client uses
* :mod:`.client`         the service client: the managed-environment operations
* :mod:`.guard`          the guard: own-ids ledger, the accepted set and the stop rules
* :mod:`.core_signin`    the lane on Crew's own sign-in (the vault, never kiro-cli)
* :mod:`.install`        Crew's install, machine mode and the boot hook on the box
* :mod:`.engine`         the ``LaunchEngine``
* :mod:`.routes`         the dashboard's loopback, owner-only routes
* :mod:`.mint_and_attach` and :mod:`.ssh_config`  the ssh ``ProxyCommand`` and its block
* :mod:`.provider`       the launch row and the platform seam's sign-in profile
"""

PROVISIONER_ID = "kiro_managed_env"
PROVISIONER_KIND = "kiro_managed_env"
