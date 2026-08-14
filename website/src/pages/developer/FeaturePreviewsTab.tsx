import { useNavigate } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'

import { SettingsCard, SettingsToggle } from '../../components/settings'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_WEBHOOKS, setPreviewFlag } from '../../utils/previewFlags'
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

  return (
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
  )
}
