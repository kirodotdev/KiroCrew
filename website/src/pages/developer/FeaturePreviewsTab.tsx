import { useNavigate } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'

import { SettingsCard, SettingsToggle } from '../../components/settings'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_CREW, PREVIEW_INSTANCE_SESSIONS, PREVIEW_REMOTE_CREW_CHAT, PREVIEW_WEBHOOKS, setPreviewFlag } from '../../utils/previewFlags'
import { i18nT } from '../../i18n/t'

/**
 * Developer > Feature Previews — opt in to surfaces that ship in the bundle but
 * are not released yet (see `utils/previewFlags.ts`).
 *
 * The USER-FACING copy says "features" and "pages", never "surfaces": `Surface`
 * is the registry's internal term and means nothing to the operator reading the
 * toggle. The component, file and catalog keys keep the code vocabulary on
 * purpose — they name the mechanism, not the copy.
 *
 * ONE CARD PER FEATURE, and everything that belongs to a feature lives inside
 * its card: the headline, the sentence explaining what state it is in, its
 * toggle, and any ingress that only appears once it is on. A reader scanning the
 * page can then take a card as the whole story of one preview, rather than
 * pairing a row against an ingress rendered somewhere below it.
 *
 * The page's own title and the "unpolished on purpose" caveat are NOT repeated
 * here: they are the tab's `label` and `description` in `DeveloperPage.tsx`, and
 * `SidePanelLayout` renders both as the page header.
 *
 * One explicit card per preview flag rather than a loop over a table: the copy
 * has to be a static `i18nT('literal')` call for `check-i18n-keys.mjs` to
 * resolve it, and a table of key strings indexed per card is exactly the dynamic
 * pattern that gate cannot follow. A preview flag is also meant to be
 * short-lived, so the cost of a card is paid once and then deleted with it.
 *
 * Deliberately NOT under `pages/settings/`: `gen-settings-registry.mjs` scans
 * that directory and would index these toggles into Settings search, which would
 * advertise the very surface the flag exists to hide.
 */
export function FeaturePreviewsTab() {
  const navigate = useNavigate()
  const webhooks = usePreviewFlag(PREVIEW_WEBHOOKS)
  const crew = usePreviewFlag(PREVIEW_CREW)
  const remoteCrewChat = usePreviewFlag(PREVIEW_REMOTE_CREW_CHAT)
  const instanceSessions = usePreviewFlag(PREVIEW_INSTANCE_SESSIONS)

  return (
    <>
    <SettingsCard>
      <SettingsToggle
        label={i18nT('pages.developer.featurePreviewsTab.remote_instance_sessions')}
        description={i18nT('pages.developer.featurePreviewsTab.merge_a_connected_remote_instances_live_sessions')}
        checked={instanceSessions}
        onChange={v => setPreviewFlag(PREVIEW_INSTANCE_SESSIONS, v)}
      />
      {/* No ingress button: this preview has no page of its own — it changes the
          Sessions list every user already looks at, so the toggle IS the whole
          affordance. A "go here to see it" link would point at the surface the
          reader is already on. */}
    </SettingsCard>
    <SettingsCard>
      <SettingsToggle
        label={i18nT('pages.developer.featurePreviewsTab.webhooks')}
        description={i18nT('pages.developer.featurePreviewsTab.inbound_webhook_tokens_registered_contexts_and_r')}
        checked={webhooks}
        onChange={v => setPreviewFlag(PREVIEW_WEBHOOKS, v)}
      />
      {webhooks && (
        <div className="pt-1">
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
        </div>
      )}
    </SettingsCard>
    {/* One card, one flag, BOTH crew doors: the Crew Members rail item and the
        sidebar's "New Crew Mode chat" entry. The toggle copy names both, because
        a reader who only sees "Crew" cannot predict which of the two moves — and
        the two appear in places far enough apart that discovering the second one
        by flipping the switch is not reliable.

        NO ingress button here, deliberately, unlike the webhooks card above. That
        one needs its link because `/webhooks` is `hiddenFromNav` and the card is
        its ONLY door. Crew is not: flipping this switch puts the Crew Members row
        back on the rail in the same tick (`usePreviewFlagRevision`), so a link
        here would be a second spelling of a door the user can already see — and
        one that costs a catalog key in twelve languages permanently. */}
    <SettingsCard>
      <SettingsToggle
        label={i18nT('pages.developer.featurePreviewsTab.crew')}
        description={i18nT('pages.developer.featurePreviewsTab.the_crew_members_page_and_crew_mode_chats_both_a')}
        checked={crew}
        onChange={v => setPreviewFlag(PREVIEW_CREW, v)}
      />
    </SettingsCard>
    {/* A SEPARATE card from Crew above, because the word names two unrelated
        things: that flag holds Crew Mode and the Crew Members page, this one
        holds a chat dispatched to another MACHINE over the instances tunnel.
        One card each keeps a reader from flipping the wrong switch.

        NO ingress button, for the same reason as the crew card: turning it on
        puts the create-menu entry back in the same tick, and that menu is
        already in front of the user. */}
    <SettingsCard>
      <SettingsToggle
        label={i18nT('pages.developer.featurePreviewsTab.chat_on_a_crew')}
        description={i18nT('pages.developer.featurePreviewsTab.chat_on_a_crew_desc')}
        checked={remoteCrewChat}
        onChange={v => setPreviewFlag(PREVIEW_REMOTE_CREW_CHAT, v)}
      />
    </SettingsCard>
    </>
  )
}
