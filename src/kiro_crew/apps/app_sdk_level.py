"""The App Kit seam level this gateway provides (adoption pins require a minimum).

Level 9 adds ``permissions.bots``: an app's backend reaches the Bots the owner ticks
for it with its app token. Level 10 adds the agent tool gate's fence on an installed
app's ``credentialMasks``, and ``permissions.chatLaunch``: an app's page opens the
person's chat it declares; and ``permissions.browserSites``: the app's own agent
browses the pages the owner approved without a card per step, and the page's
``useBrowserMedia`` controls that tab's media through ``POST /api/browser/media``.
``ctx.embed``'s batch members share level 10.
Level 11 adds the page's ``BrowserView``, ``useBrowserView`` and ``useBrowserCoords``:
the live view of the app's own chat slot, with take over and hand back, behind the
``app_browser_sdk`` preview; and the SDK's ``appWebOriginStatus``.
Level 12 adds ``permissions.kiroOperations``: ``POST /api/apps/<name>/kiro/<operation>``
and ``ctx.kiro_call`` call the Kiro operations the owner approved for the app, with
the person's Kiro sign-in, which never reaches the app; and the SDK's
``SessionChanges`` and ``DiffView``.
Level 13 adds ``/api/apps/<name>/browser-sites``: the app's browsing list with the
sites the owner added, which the app and its backend read, and the owner's edits to
it; and the SDK's ``useBrowserSites``, whose add asks the owner in the dashboard.
Level 14 adds backend SDK names, one batch: ``canonical_file_path``, the gateway's
file path gate (the UNC and link screens and the sensitive-path list, answered as the
canonical path or None); and ``vet_public_url``, ``UrlRejectedError`` and
``address_is_not_public``, the gateway's outbound public-address check for a URL, or
a resolved address, an app's backend fetches. Level 14 also adds ``sniff_raster_mime``
and ``RASTER_SNIFF_BYTES``: the gateway's own raster sniffer, by reference. Level 14
also adds the descriptor-pinned path walk and copy: ``supports_pinned_walk``,
``is_reparse_point``, ``pin_parent``, ``copy_file_pinned``, ``fd_real_path`` and
``PinnedPathRefusal``.

Level 15 adds ``permissions.browserRecipes``: fixed clicks on CSS selectors the gateway
runs on the app's own agent's tab, through ``POST /api/browser/media`` (``action:
"recipe"``, the SDK's ``useBrowserMedia().runRecipe``) and, for a ``watch`` recipe, by
itself while the app's media plays.
Level 16 adds the SDK's ``useKeepAlive``: a routed page stays mounted, hidden, while
the app says it is busy, and the dashboard names it on every page with Open and Stop.

Level 17 adds backend SDK names, one batch: ``iter_credential_matches`` and
``CREDENTIAL_PLACEHOLDER``, the gateway's credential scan over text an app holds and
the text its redactor writes; ``contains_exfiltration_url``, its exfiltration-URL
check, answered as a bool; ``DENIED_ROOT_PARTS``, the credential directory names
refused at any depth of a picked folder; ``minimal_env``, the location-hints-only
environment; ``git_hook_disable_args``, ``git_hook_disable_args_async`` and
``GitHookScanError``, the overrides that turn off a repository's config-defined git
hooks, listed by a sandboxed git; ``max_output_bytes`` on ``run_sandboxed_async`` with
``OutputLimitExceeded``; and ``record_or_raise`` on the audit writer (``ctx.audit``
and ``audit_log``), the fail-closed row, written only under the name the gateway
gave the app.

Level 18 adds backend SDK names, one batch: ``trusted_git_bin``,
``trusted_system_bin``, ``uv_executable`` and ``nvm_version_bin_dirs``, the gateway's
own lookups for the tools it runs; ``agent_template_host_values``, the values it
renders for the agent-template placeholders that name no app;
``address_covers_loopback``, the bind-address predicate; and ``extra_hidden_dirs`` on
the spawn names, more paths hidden from a child and nothing lifted.
"""

APP_SDK_LEVEL = 18
