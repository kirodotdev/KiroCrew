"""The App Kit seam level this gateway provides (adoption pins require a minimum).

Level 9 adds ``permissions.bots``: an app's backend reaches the Bots the owner ticks
for it with its app token. Level 10 adds the agent tool gate's fence on an installed
app's ``credentialMasks``, and ``permissions.chatLaunch``: an app's page opens the
person's chat it declares; and ``permissions.browserSites``: the app's own agent
browses the pages the owner approved without a card per step, and the page's
``useBrowserMedia`` controls that tab's media through ``POST /api/browser/media``.
"""

APP_SDK_LEVEL = 10
