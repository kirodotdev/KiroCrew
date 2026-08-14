import { useNavigate } from 'react-router-dom'
import { Webhook, ArrowRight } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { SettingsSection, SettingsCard } from '../../components/settings'
import { Badge, Btn } from '../../components/ui'
import { api, type WebhooksView } from '../../api/client'
import { i18nT } from '../../i18n/t'

/**
 * Settings entry point for inbound webhooks.
 *
 * Deliberately a summary rather than the whole surface: the Webhooks page is a
 * rail-and-detail shell with its own resizable splitter, and Settings is itself
 * a `SidePanelLayout`. Embedding one in the other would stack two rails and
 * spend most of the width on chrome. So this panel answers the questions a
 * settings reader actually has — is it on, how many callers can reach it — and
 * hands off to the full page for the work.
 */
export function WebhooksPanel() {
  const navigate = useNavigate()

  // Partial `api/client` mocks are common in this suite, so a missing method
  // must not throw on mount. Same defensive shape the other panels use.
  const { data, isLoading, isError } = useQuery<WebhooksView | null>({
    // Shares the page's `webhooks` PREFIX, so the page invalidating ['webhooks']
    // after minting or revoking a token refreshes this badge too — but keeps its
    // own leaf, because the page's queryFn substitutes an empty view where this
    // one wants null to tell "unreachable" apart from "unconfigured".
    queryKey: ['webhooks', 'settings-summary'],
    queryFn: () => Promise.resolve(api.webhooks?.()).then(v => v ?? null),
  })

  const tokenCount = data?.tokens?.length ?? 0
  // Badges deliberately borrow the full page's own strings
  // (`pages.webhooksPage.*`) rather than defining panel copies: this card is one
  // click from that page, and a state renamed on arrival costs the reader a
  // re-orientation every visit.
  //
  // `enabled` is the effective state (has_tokens && switch_on); `switch_on` is
  // the kill switch alone. They differ on a fresh install with the switch on
  // and no tokens yet, which is the common first-run case — so report the
  // reason rather than a bare "off".
  const state = !data
    ? 'unknown'
    : data.enabled
      ? 'on'
      : data.switch_on
        ? 'no-tokens'
        : 'switched-off'

  return (
    <SettingsSection title={i18nT('pages.settings.webhooksPanel.inbound_webhooks')}>
      <SettingsCard>
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1 min-w-0">
            <div className="flex items-center gap-2">
              <Webhook size={16} className="text-muted shrink-0" />
              <span className="text-sm font-medium text-text-strong">
                {i18nT('pages.settings.webhooksPanel.endpoint_status')}
              </span>
              {!isLoading && !isError && state === 'on' && (
                <Badge variant="ok">{i18nT('pages.webhooksPage.on')}</Badge>
              )}
              {!isLoading && !isError && state === 'switched-off' && (
                <Badge variant="muted">{i18nT('pages.webhooksPage.off')}</Badge>
              )}
              {!isLoading && !isError && state === 'no-tokens' && (
                <Badge variant="muted">{i18nT('pages.webhooksPage.no_credential_yet')}</Badge>
              )}
            </div>
            <p className="text-[13px] text-muted">
              {i18nT('pages.settings.webhooksPanel.lets_an_outside_system_start_one_agent_turn')}
            </p>
            {!isLoading && !isError && tokenCount > 0 && (
              // Suppressed at zero: the badge already says "No tokens yet", and
              // "0 access tokens" under it just repeats that.
              <p className="text-[12px] text-muted">
                {i18nT('pages.settings.webhooksPanel.tokens_count', { count: tokenCount })}
              </p>
            )}
            {isError && (
              // Say the read failed rather than rendering a stateless card. Without
              // this the badge and count both vanish and the panel looks the same
              // as a healthy endpoint with nothing configured — indistinguishable
              // from a gateway that is simply unreachable.
              <p className="text-[12px] text-warn">
                {i18nT('pages.settings.webhooksPanel.could_not_read_webhook_status')}
              </p>
            )}
          </div>
          <Btn onClick={() => navigate('/webhooks')} className="shrink-0">
            {i18nT('pages.settings.webhooksPanel.manage_webhooks')}
            <ArrowRight className="lucide-inline" />
          </Btn>
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}

export default WebhooksPanel
