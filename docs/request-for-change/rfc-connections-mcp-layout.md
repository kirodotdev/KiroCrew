---
title: Connection cards and OAuth apps on one page, MCP Servers on the rail, sharing up top
status: accepted
author: iamwhatever
created: 2026-11-10
last-audited: 2026-11-10
audited-at: 8f6fe6dbfe
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Connection cards and OAuth apps on one page, MCP Servers on the rail, sharing up top

- Status: accepted. These are the product owner's decisions, given
  2026-11-10, for a personal single-user tool. Claims about today's code were
  checked at `8f6fe6dbfe` (main).
- Author: iamwhatever

## 1. Today

| Thing | Route | Component / file |
|---|---|---|
| Customize → Connections (provider cards) | `/capabilities?tab=mcp` (sub-tab Services, no `view`) | `pages/CapabilitiesPage.tsx` tab `mcp` → `pages/connections/ConnectionsPage.tsx`, cards in `pages/connections/ConnectionsPageCard.tsx` |
| Settings → OAuth Apps (client id / secret per provider) | `/settings/connections` | `pages/settings/ConnectionsPanel.tsx` (`ClientCard` per pre-registered provider), mounted by `pages/SettingsPage.tsx` tab `connections` |
| MCP servers list | `/capabilities?tab=mcp&view=mcp-servers` (`MCP_SERVERS_HREF`) | second sub-tab of `ConnectionsPage.tsx`, list in `pages/overview/McpTab.tsx`, sub-tab state in `pages/connections/useConnectionsSubTab.ts` |
| MCP sharing | `/capabilities?tab=mcp&view=mcp-servers#mcp-management` | `pages/settings/McpManagement.tsx` in a section **below** the whole server list; hero switch in `McpManagementHero.tsx`; per-server Share switches, npm pre-install and the assessment sit in a collapsed **Advanced** (`advancedOpen`, default closed) |

Paths are under `website/src/`. What a reader sees (no screenshots in
this doc):

1. Customize opens a rail. The row labelled "Connections" holds two
   sub-tabs, Services and MCP Servers.
2. A card for GitHub, Asana, Google … whose OAuth app is missing shows a
   "Configure OAuth app" link. The link leaves the page for
   `settingsPath({ tab: 'connections', highlight })`, a Settings tab whose
   label reads "OAuth Apps" (`settings.tabs.connections.label`). You fill in
   the client there, then come back to press Connect.
3. Google is six cards and six OAuth app entries (`registry.json`, one
   redirect port each). One Google client serves all six
   (`docs/guides/oauth-app-registration/README.md`), but you paste it six
   times.
4. MCP servers are a sub-tab of a tab of Customize, two clicks deep.
5. The page-wide sharing switch is shown, but only after you scroll past
   every installed server. The per-server Share switches are one more click
   behind "Advanced".

Other places link in: `ConnectionsRedirect` in `shell/routes.tsx` (`/connections`
→ `/capabilities?tab=mcp`), `MCP_SERVERS_HREF` used by
`pages/developer/AgentBackendTab.tsx`, `pages/settings/McpManagementAssessment.tsx`,
`McpShareRefusedNotice.tsx`, `DeveloperPanel.tsx`, `developerSubs.ts`,
`DeveloperPage.tsx` and `guide/guideActions.ts`; the Secrets panel "owned
by OAuth Apps" link (`SecretsPanel.tsx`); the command palette's one entry
per pre-registered provider (`settingsManual.ts`, tab `connections`) and its
generated files `settingsRegistry.gen.ts` and
`src/kiro_crew/docs/settings-registry.generated.json`. The backend and docs
hold more of these links: `src/kiro_crew/data/tips_curated.json`,
`src/kiro_crew/doctor_checks/mcp.py`, `src/kiro_crew/docs/{agents,mcp-apps,troubleshooting,connections}.md`,
`docs/guides/oauth-app-registration/*.md`, `docs/guides/secrets-env.md`
and `docs/system-specs/modules/connections.md`.

## 2. Decision

The owner clarified this on 2026-11-10, and it binds the code PR.

1. **One Connections page.** The Connection cards from Customize →
   Connections move into the OAuth Apps area, and the two become one page.
   It is the Settings tab `connections` (`/settings/connections`), relabelled
   from "OAuth Apps" to "Connections". The Customize tab `mcp` is removed
   from `CapabilitiesPage.tsx`, along with its pinnable `capabilities-mcp`
   rail row (`CAPABILITY_SUB_ITEM_NAV`).
2. **A card expands in place.** Clicking a card opens its configuration
   inline on that card: status, Connect / Disconnect / Test, and, for a
   pre-registered provider, today's `ClientCard` (redirect URI copy, Client
   ID, Client secret, the env-override note, the runbook link). There is no
   separate page and no modal per card. A card whose client is missing opens
   that part first, so the "Configure OAuth app" link becomes the expand.
   `ClientCard` moves to `pages/connections/OAuthAppPanel.tsx`. The list
   wrapper `ConnectionsPanel` is deleted.
3. **One Google entry.** The six Google cards (`gmail`, `google-drive`,
   `google-calendar`, `google-sheets`, `google-docs`, `google-slides`)
   become one Google entry. The user fills in the Google client once.
   - The expanded entry lists all six redirect URIs to copy, and one
     Connect / Disconnect and status row per product.
   - It links to the setup script (`scripts/google-oauth-setup.sh`, and
     `.ps1` on Windows) and to the private-app guide
     (`docs/guides/oauth-app-registration/google.md`). Both come from
     [#34290](https://github.com/kirodotdev/KiroCrew/pull/34290), not the
     page PR.
   - Saving follows §2.8.
4. **MCP Servers gets its own rail row.** A new built-in surface `mcp`
   (route `/mcp`, label "MCP Servers", group Bottom, between Customize and
   Settings) renders today's MCP Servers sub-tab as a page. The
   Services / MCP Servers sub-tab strip and `useConnectionsSubTab.ts` go away.
5. **Sharing is at the top of `/mcp`.** The page is laid out top to bottom
   like this:
   - The sharing hero: page-wide switch, measured numbers and the
     open-chats banner.
   - The approval card.
   - Two views, **Servers** and **Sharing**, using the `?view=` convention.
     Servers is today's `McpTab` list. Sharing is today's Advanced body,
     always open: the per-server Share table, npm pre-install and the
     assessment.

   The hero and the view strip sit above the list. So sharing stays in
   view however many servers are installed. The Advanced disclosure and
   `initialAdvancedOpen` are removed.
6. **Old links keep working.** Every redirect is a replace-navigation, and
   query and hash are kept:

| Old | New |
|---|---|
| `/capabilities?tab=mcp&view=mcp-servers` | `/mcp` |
| `/capabilities?tab=mcp&view=mcp-servers#mcp-management` | `/mcp?view=sharing` |
| `/capabilities?tab=mcp` (no view) | `/settings/connections` |
| `/connections` | `/settings/connections` (`ConnectionsRedirect`) |
| `/settings/connections?highlight=connections.oauth-client-<slug>` | same route. The highlight expands that card. A Google slug expands the Google entry |
| `/settings?tab=connections` (legacy query form) | unchanged: the existing legacy translation |

   `?provider=<slug>` on `/settings/connections` expands and scrolls to a
   card. It seeds the existing `highlightedSlug` state, which today is set
   only by clicks. `MCP_SERVERS_HREF` becomes `/mcp`, and
   `MCP_MANAGEMENT_PATH` becomes `/mcp?view=sharing`. Every in-repo writer
   listed in §1 is re-pointed in the same code PR, so no in-repo link relies
   on a redirect. The redirects are for bookmarks, old chats and old tips.
7. **The palette.** The per-provider "OAuth app — <Provider>" entries
   (`settingsManual.ts`) stay on tab `connections`. The six Google entries
   fold into one "OAuth app — Google".

8. **Shared Google client.** This item is taken from
   [#34295](https://github.com/kirodotdev/KiroCrew/pull/34295), closed as a
   duplicate of this RFC.
   - **Shared save is on by default.** A Google slug is `gmail` or starts
     with `google-`. One Save sends the same
     `PUT /api/connections/oauth-clients/<slug>` body to all six Google
     slugs, one after another. A Client ID or a cleared secret reaches all
     six the same way. A partial failure names the products that failed and
     offers a retry.
   - **The opt-out.** "Use for every Google card" can be turned off, and
     then a save writes one slug alone. Remove stays per slug.
   - **Storage is unchanged.** Each slug keeps its own record
     (`connections.oauth_clients.<slug>.client_id`, the vault's
     `CONNECTIONS_<SLUG>_CLIENT_SECRET`), and environment overrides still
     win. A person who saved different clients per product keeps them until
     the next shared save.
   - **Upstream revoke.** Disconnect revokes the Google grant upstream only
     when the last Google card on the same client id AND the same account
     disconnects. While another Google slug holds a grant from the same
     issuing client and account, Disconnect deletes only its own entry
     (`revoked: false, shared: true`). An unknown account or client counts
     as not shared, so the revoke happens. This is already on main
     (`_shares_upstream_grant` in `src/kiro_crew/connections/crew_oauth.py`).
     The page shows it and must not change it.
9. **Who builds what.**
   - [#34290](https://github.com/kirodotdev/KiroCrew/pull/34290) builds the
     shared save on today's `ConnectionsPanel.tsx`, plus the guide, the
     scripts and the registry `registration_guide` links.
   - chat-825's page PR (work item `it_f44f2e75`) builds §2.1–2.7 on top of
     it: moving the cards, the one Google entry, `/mcp`, sharing at the
     top, and the redirects.

## 3. Not decided here

- What connecting, the server list or the sharing controls do stays
  unchanged. So do every `/api/connections/*` and MCP gateway endpoint, and
  secret custody (the vault, env override). This RFC moves UI.
- Whether the backend later stores one Google client instead of six copies.

## 4. i18n keys

Each key changes in all 14 catalogs under `website/src/i18n/locales/`,
including `en.manual.json` and the pseudolocale.

- New: `nav.mcp_servers`; `pages.mcpPage.title`, `.view_servers` and
  `.view_sharing`; `pages.connectionsPage.google_entry_name`,
  `.google_redirect_uris`, `.google_setup_script`, `.google_private_app_guide`;
  `pages.settings.connectionsPanel.oauth_app_google`. The shared-save
  toggle and partial-save strings come with #34290.
- Reworded: `settings.tabs.connections.label` ("OAuth Apps" → "Connections")
  and `.description`.
- Reused as is: every other `pages.settings.connectionsPanel.*` string that
  `ClientCard` renders, and the `pages.connectionsPage.*` card strings.
- Removed: `pages.capabilitiesPage.connections_label` and
  `.connections_description` (the Customize tab);
  `pages.connectionsPage.services`, `.mcp_servers` and `.connection_views`
  (the sub-tab strip); `pages.connectionsPage.mcp_management_heading`;
  `pages.mcpManagement.advanced`; `pages.connectionsPage.configure_oauth_app`
  (becomes the expand); `pages.settings.connectionsPanel.title` and `.intro`
  (the old list header).
- `dynamic-keys-baseline.json`, `untranslated-baseline.json` and
  `render-baseline.json` move only if a gate says so.

## 5. Tests

| Change | Files |
|---|---|
| Moved / rewritten | `pages/settings/ConnectionsPanel.test.tsx` → `pages/connections/OAuthAppPanel.test.tsx`; `test/ConnectionsPage.subTabUrl.test.tsx` → `/mcp` view tests |
| Advanced removed | `test/McpManagement*.test.tsx` (8 files) drop `initialAdvancedOpen`; `test/McpManagementMove.test.tsx` asserts sharing renders above the list |
| Link targets | `test/ConnectionsPage.coverage.test.tsx`, `test/ConnectionsPage.searchMemory.test.tsx`, `test/McpShareRefusedNotice.test.tsx`, `test/AgentBackendTab.detailCard.test.tsx`, `test/DeveloperPanel*.test.tsx` |
| Routing / shell | `test/App.shell.routes.test.tsx` (new redirects), `test/SettingsPage.agentHarness.test.tsx`, `test/settingsCoverage.test.ts`, `test/previewSurfaces.test.tsx`, nav-rail and pin tests (new `mcp` row, `capabilities-mcp` gone) |
| Guide / UI index | `guide/GuideLayer.*.test.tsx`, `uiLocations/*.test.ts`; regenerate `uiLocations/guidePlans.gen.ts`, `settingsRegistry.gen.ts`, `src/kiro_crew/docs/ui-index.generated.json`, `src/kiro_crew/docs/settings-registry.generated.json` |
| Backend | `test/test_doctor_mcp_share_refused.py`, `test/test_find_ui*.py`, `test/test_guide_ui_find.py`, `test/gui_user/features.json` |

New tests:
- One redirect test per row of the §2.6 table.
- Clicking a card expands its config inline; a card with a missing client
  opens on the OAuth app part.
- The Google entry renders once for six slugs, saves to all six by
  default, names the failed products on a partial save, and shows both links.

## 6. Size of the code PR

There is one code PR, frontend-heavy, about 1,100–1,500 changed lines,
half of them in tests. The parts:

- New `/mcp` page shell plus the surface row: about 150 lines.
- Cards moved into the Settings tab with inline expand: about 200 lines.
- The Google entry: about 150 lines.
- Sub-tab, Customize tab and Advanced removal: about −250 lines.
- Redirects: about 60 lines.
- About 25 link writers re-pointed.
- 14 catalogs: about 12 keys each.
- Doc and tip URL updates, and generated-file regeneration.

`feature-map/README.md` lists the new component files.
