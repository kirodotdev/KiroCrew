import { useNavigate } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'

import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { FeaturePreviewIntroButton, type FeaturePreviewIntro } from '../../components/FeaturePreviewIntroDialog'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_CREW, PREVIEW_REMOTE_CREW_CHAT, PREVIEW_WEBHOOKS, setPreviewFlag } from '../../utils/previewFlags'
import { i18nT } from '../../i18n/t'

/**
 * Settings > Developer > Feature Previews — opt in to surfaces that ship in the
 * bundle but are not released yet (see `utils/previewFlags.ts`).
 *
 * Formerly its own tab on the standalone Developer page (`/developer`). It moved
 * here because the switch that HOLDS an unreleased feature belongs next to the
 * switch that REVEALS the developer tooling (Developer Mode, one section up):
 * both are per-device consent gates, and a reader looking for "how do I turn
 * the unfinished thing on" looks in Settings, not on an internals page they
 * first have to unlock. `DeveloperPage.tsx` redirects the old
 * `/developer?tab=feature-previews` link here.
 *
 * The USER-FACING copy says "features" and "pages", never "surfaces": `Surface`
 * is the registry's internal term and means nothing to the operator reading the
 * toggle. The component and catalog keys keep the code vocabulary on purpose —
 * they name the mechanism, not the copy. The catalog keys also keep their
 * historical `pages.developer.featurePreviewsTab.*` namespace: renaming seven
 * keys across fourteen catalogs buys no user-visible change, and the section
 * itself still describes the developer-tooling gate it started as.
 *
 * ONE CARD PER FEATURE, and everything that belongs to a feature lives inside
 * its card: the headline, the sentence explaining what state it is in, its
 * toggle, and any ingress that only appears once it is on. A reader scanning the
 * section can then take a card as the whole story of one preview, rather than
 * pairing a row against an ingress rendered somewhere below it.
 *
 * One explicit card per preview flag rather than a loop over a table: the copy
 * has to be a static `i18nT('literal')` call for `check-i18n-keys.mjs` to
 * resolve it, and a table of key strings indexed per card is exactly the dynamic
 * pattern that gate cannot follow. A preview flag is also meant to be
 * short-lived, so the cost of a card is paid once and then deleted with it.
 *
 * Under `pages/settings/` ON PURPOSE, reversing the old tab's stance:
 * `gen-settings-registry.mjs` scans this directory, so the three toggles ARE
 * indexed into Settings search (`PANEL_TAB_MAP` maps this file to `developer`).
 * The old tab kept itself out of the index so that searching "webhooks" would
 * not advertise a hidden page. In Settings the calculus flips: a control the
 * user can see on a Settings pane but cannot find through Settings search is
 * the exact coverage gap the settings-coverage gate exists to close, and what
 * the search hit reaches is the opt-in switch — labelled as a preview — not the
 * page it holds. The PAGE stays un-advertised: `getAdvertisedSurfaces()` and
 * the Search Everywhere Pages provider still filter it until the flag is on.
 *
 * No `configKey` on these toggles, deliberately. That prop names a
 * `config.json` path so `<SettingRef>` chips can deep-link, and preview flags
 * are per-device localStorage keys by design (`previewFlags.ts` explains why
 * they are NOT backend config). Search deep-links still reach each toggle
 * through its registry id + `data-setting-label`, which need no configKey.
 *
 * Each card may also carry a "See what it looks like" button (`FeaturePreviewIntroButton`)
 * opening a dialog with a REAL capture of the surface the flag reveals, a
 * sentence on what it does, where it appears once on, and the same switch again.
 * The intro is defined right here next to its card — the card IS the preview's
 * definition — as a render-time builder rather than a table of catalog keys,
 * for the same `check-i18n-keys.mjs` reason the cards are not a loop: the gate
 * resolves a literal `i18nT('…')`, not a key read out of a nested object. A
 * preview whose surface has not been captured yet (below: "Chat on a crew",
 * whose menu entry only exists with a live tunnel to a second machine, which an
 * isolated capture instance cannot honestly stage) simply has no builder and so
 * no button — never a dialog with an empty frame.
 */

/** Public paths of the captures; the files ride `public/`, never a JS chunk. */
const MEDIA_BASE = '/app-assets/feature-previews'

/** Webhooks: the `/webhooks` page itself, the flag's only door. */
function webhooksIntro(): FeaturePreviewIntro {
  return {
    summary: i18nT('pages.developer.featurePreviewsTab.intro.webhooks_summary'),
    whereToFind: i18nT('pages.developer.featurePreviewsTab.intro.webhooks_where'),
    media: [
      {
        kind: 'image',
        light: `${MEDIA_BASE}/webhooks-page-light.png`,
        dark: `${MEDIA_BASE}/webhooks-page-dark.png`,
        caption: i18nT('pages.developer.featurePreviewsTab.intro.webhooks_media_page'),
      },
    ],
  }
}

/** Crew Members: the `/members` page, the flag's only door. */
function crewIntro(): FeaturePreviewIntro {
  return {
    summary: i18nT('pages.developer.featurePreviewsTab.intro.crew_summary'),
    whereToFind: i18nT('pages.developer.featurePreviewsTab.intro.crew_where'),
    media: [
      {
        kind: 'image',
        light: `${MEDIA_BASE}/crew-members-light.png`,
        dark: `${MEDIA_BASE}/crew-members-dark.png`,
        caption: i18nT('pages.developer.featurePreviewsTab.intro.crew_media_members'),
      },
    ],
  }
}

/**
 * `data-setting-key` anchor on the section wrapper, for
 * `?highlight=key:<this>` — the redirect target of the old Developer-page tab
 * (`DeveloperPage.tsx`). Not a config path and not a registry entry: it only
 * exists so useSettingHighlight's direct DOM lookup can ring the whole section.
 * Neither the settings extractor (which reads `configKey` props on Settings*
 * primitives) nor the SettingRef call-site guard (which scans `<SettingRef>`)
 * sees a bare `data-setting-key` attribute, so it cannot leak into search or
 * pose as a schema path.
 */
export const FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR = 'feature-previews-section'
export function FeaturePreviewsSection() {
  const navigate = useNavigate()
  const webhooks = usePreviewFlag(PREVIEW_WEBHOOKS)
  const crew = usePreviewFlag(PREVIEW_CREW)
  const remoteCrewChat = usePreviewFlag(PREVIEW_REMOTE_CREW_CHAT)

  return (
    // The wrapper exists for the legacy redirect: `?highlight=key:<anchor>`
    // rings whatever element carries that `data-setting-key`, so this rings
    // the WHOLE section — header, caveat and all three cards — rather than one
    // card. A reader arriving from an old Feature Previews bookmark asked a
    // section-sized question ("where did the tab go?"), and a single ringed
    // row answers a different one ("is this row selected?"). The wrapper takes
    // over the between-sections `mt-4` because SettingsSection's own
    // `first:mt-0` now sees its header as the first child of this div.
    <div data-setting-key={FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR} className="mt-4">
    <SettingsSection title={i18nT('pages.settings.developerPanel.feature_previews')}>
      {/* The "unpolished on purpose" caveat sits under the section header rather
          than repeated per card: it is true of every card here, and the old tab
          carried it once as the page description for the same reason. */}
      <p className="text-[13px] text-muted mb-2">
        {i18nT('pages.settings.developerPanel.feature_previews_desc')}
      </p>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.developer.featurePreviewsTab.webhooks')}
          description={i18nT('pages.developer.featurePreviewsTab.inbound_webhook_tokens_registered_contexts_and_r')}
          checked={webhooks}
          onChange={v => setPreviewFlag(PREVIEW_WEBHOOKS, v)}
        />
        {/* One action row under the toggle: "See what it looks like" always, the ingress
            link only once the flag is on. Same row so the card keeps one
            footer whichever state it is in, rather than a link appearing on a
            new line and pushing the next card down. */}
        <div className="flex flex-wrap items-center gap-x-4 pt-1">
          <FeaturePreviewIntroButton
            title={i18nT('pages.developer.featurePreviewsTab.webhooks')}
            intro={webhooksIntro()}
            checked={webhooks}
            onChange={v => setPreviewFlag(PREVIEW_WEBHOOKS, v)}
          />
          {webhooks && (
            <button
              type="button"
              onClick={() => navigate('/webhooks')}
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent bg-transparent border-none cursor-pointer px-0 py-1 hover:underline"
            >
              {i18nT('pages.developer.featurePreviewsTab.open_webhooks')}
              {/* An in-app arrow, NOT `ExternalLink`: this navigates in the same
                  tab. Elsewhere in the dashboard the external-link glyph is
                  reserved for pop-outs and off-site URLs, so using it here would
                  promise a new window that never opens. */}
              <ArrowRight size={13} className="lucide-inline" />
            </button>
          )}
        </div>
      </SettingsCard>
      {/* One card, one flag, one door: the Crew Members page (`/members`) and its
          rail item. Crew Mode — the second door this card used to name — retired
          in favour of that page; the sidebar create menu keeps a "Crew Members"
          entry that opens the page, or lands HERE with this card ringed while the
          flag is still off (`ChatSidebar.openCrewMembers`).

          NO ingress button here, deliberately, unlike the webhooks card above. That
          one needs its link because `/webhooks` is `hiddenFromNav` and the card is
          its ONLY door. Crew Members is not: flipping this switch puts the row back
          on the rail in the same tick (`usePreviewFlagRevision`), so a link here
          would be a second spelling of a door the user can already see — and one
          that costs a catalog key in twelve languages permanently. */}
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.developer.featurePreviewsTab.crew_members')}
          description={i18nT('pages.developer.featurePreviewsTab.crew_members_desc')}
          checked={crew}
          onChange={v => setPreviewFlag(PREVIEW_CREW, v)}
        />
        {/* "See what it looks like" is not an ingress: it shows the page instead of
            opening it, which is what a reader deciding whether to flip the switch
            needs BEFORE flipping it. */}
        <div className="pt-1">
          <FeaturePreviewIntroButton
            title={i18nT('pages.developer.featurePreviewsTab.crew_members')}
            intro={crewIntro()}
            checked={crew}
            onChange={v => setPreviewFlag(PREVIEW_CREW, v)}
          />
        </div>
      </SettingsCard>
      {/* A SEPARATE card from Crew Members above, because the word names two
          unrelated things: that flag holds the Crew Members page, this one holds
          a chat dispatched to another MACHINE over the instances tunnel. One card
          each keeps a reader from flipping the wrong switch.

          NO ingress button, for the same reason as the crew card: turning it on
          puts the create-menu entry back in the same tick, and that menu is
          already in front of the user.

          NO "See what it looks like" yet either, and that is the missing-capture rule, not
          an omission: the entry it adds only renders while a tunnel to a second
          machine is live (`warmCrews.length > 0` in ChatSidebar), and the
          isolated instance the captures come from has no honest way to stage
          one. A dialog with a staged or drawn frame would break the promise the
          other two dialogs make — that what you see is what will appear. Add a
          builder here the day a real two-machine capture exists. */}
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.developer.featurePreviewsTab.chat_on_a_crew')}
          description={i18nT('pages.developer.featurePreviewsTab.chat_on_a_crew_desc')}
          checked={remoteCrewChat}
          onChange={v => setPreviewFlag(PREVIEW_REMOTE_CREW_CHAT, v)}
        />
      </SettingsCard>
    </SettingsSection>
    </div>
  )
}
