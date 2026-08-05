import { SlidersHorizontal } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'
import {
  PrivacyCommandList,
  PrivacyDisclosureSections,
  TelemetryToggle,
} from '../../components/PrivacyDisclosure'
import { i18nT } from '../../i18n/t'

/** Durable disclosure surface. This page explains controls and offers the
 * in-product opt-out, but does not ask for consent or gate use of the
 * application.
 *
 * The disclosure copy, the toggle, and the command list live in
 * `components/PrivacyDisclosure.tsx` because the onboarding privacy step renders
 * the same three pieces — single-sourcing them is what keeps the first-run
 * explanation and this durable panel from drifting apart. */
export function PrivacyPanel() {
  return (
    <div aria-label={i18nT('privacyDisclosure.settingsLabel')}>
      <Card>
        <PrivacyDisclosureSections />
      </Card>

      <Card>
        <CardTitle>
          <SlidersHorizontal className="lucide-inline" aria-hidden="true" />
          {i18nT('privacyDisclosure.controlsTitle')}
        </CardTitle>
        <TelemetryToggle />
        <p className="text-sm text-muted leading-relaxed mt-4 mb-3">
          {i18nT('privacyDisclosure.controlsBody')}
        </p>
        <PrivacyCommandList />
      </Card>
    </div>
  )
}
