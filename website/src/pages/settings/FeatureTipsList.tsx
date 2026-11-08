import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { tipActionRoute } from '../../components/TipCard'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/**
 * Settings → Chat → Discovery: every curated tip, dismissed ones included, so a
 * tip seen once (or closed) can be found again. Opening a tip's action sends no
 * tip feedback, so browsing here never changes which tip the composer shows.
 * Renders nothing while loading or when the pool is empty (`tips_enabled` off,
 * or an edition that ships no tips).
 */
export function FeatureTipsList() {
  const navigate = useNavigate()
  const q = useQuery({ queryKey: ['tipsAll'], queryFn: () => api.tipsAll() })
  // No hand-off: ChatPanel's `localRoleOther` / `localBudget` / `localKeepChars`
  // drafts would be unmounted by the navigation.
  if (q.isError) {
    return <ErrorNotice variant="inline" message={i18nT('pages.settings.featureTipsList.failed_to_load')} />
  }
  const tips = q.data?.tips ?? []
  if (!tips.length) return null
  return (
    <SettingsSection title={i18nT('pages.settings.featureTipsList.title')} hint={i18nT('pages.settings.featureTipsList.hint')} collapsible>
      <SettingsCard>
        <ul data-testid="feature-tips-list" className="list-none m-0 p-0 flex flex-col gap-2.5">
          {tips.map(tip => {
            const route = tipActionRoute(tip.action)
            return (
              // Wraps the button under the text on a narrow pane rather than squeezing the text.
              <li key={tip.id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-1.5">
                <div className="flex-1 basis-64 min-w-0">
                  <div className="text-[13px] font-semibold text-text">{tip.title}</div>
                  <div className="text-[12px] text-muted mt-0.5 break-words [&_p]:m-0">
                    <MarkdownRenderer content={tip.body} />
                  </div>
                </div>
                {route && tip.action && <Btn onClick={() => navigate(route)}>{tip.action.label}</Btn>}
              </li>
            )
          })}
        </ul>
      </SettingsCard>
    </SettingsSection>
  )
}
