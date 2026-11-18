/**
 * Home's "Needs you" panel. Blockers (question cards, tool approvals, sessions
 * marked needs-input) get the All Dashboards page's own `AttentionCard`, so a
 * decision taken here routes exactly as it would there. Follow-up suggestions
 * (an idle session whose newest reply ended in `[OPTIONS:]`) are one compact row
 * each behind a fold: nothing is stuck, and a full card per offer buried the
 * blockers under a screen of buttons.
 */
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Inbox } from 'lucide-react'
import { Btn, Card, EmptyState, PanelSectionHeader } from '../../components/ui'
import AttentionCard, { RestoredQuestionNotice } from '../chat/command-center/AttentionCard'
import { runTitle, type AttentionItem, type RunNode } from '../chat/command-center/model'
import type { CommandCenterData } from '../chat/command-center/useCommandCenter'
import { fmtNumber } from '../../i18n/format'
import type { AttentionSplit } from './homeModel'
import { ListRow, RowLink } from './homeRows'

/** Blocker cards per step: one row of the widest grid at first, and each
 *  "Show more" adds another row, so the inbox grows a row at a time instead of
 *  all at once. */
const BLOCKERS_PER_ROW = 3

function SuggestionRow({ item, title }: { item: AttentionItem; title: string }) {
  const { t } = useTranslation()
  // Labels shown plus the command-shaped ones the model withholds from a bare send.
  const choices = (item.question?.questions[0]?.options.length ?? 0) + (item.sessionOnlyOptions ?? 0)
  return <ListRow className="py-1.5" data-testid="home-suggestion-row">
    <div className="flex-1 min-w-0">
      <div className="text-sm truncate">{title}</div>
      <div className="text-[12px] text-muted truncate">{t('pages.homePage.suggestion_choices', { count: choices, amount: fmtNumber(choices) })}</div>
    </div>
    <RowLink to={`/chat?sid=${encodeURIComponent(item.slot)}`}>{t('pages.homePage.open')}</RowLink>
  </ListRow>
}

export default function HomeNeedsYou({ data, attention, ready }: { data: CommandCenterData; attention: AttentionSplit; ready: boolean }) {
  const { t } = useTranslation()
  const [visible, setVisible] = useState(BLOCKERS_PER_ROW)
  const sessionBySlot = new Map(data.nodes.filter(n => n.kind === 'session').map(n => [n.slot, n] as const))
  const title = (slot: string) => { const node = sessionBySlot.get(slot); return node ? runTitle(node) : slot }
  const detail = (slot: string): RunNode['detail'] => sessionBySlot.get(slot)?.detail
  const { blockers, suggestions } = attention
  const shown = blockers.slice(0, visible)
  const hidden = blockers.length - shown.length
  const nextStep = Math.min(hidden, BLOCKERS_PER_ROW)
  const empty = ready && !data.stale && !blockers.length && !suggestions.length && !data.restoredQuestionNotices.length
  return <section aria-label={t('commandCenter.attention_filter')} className="space-y-3" data-testid="home-needs-you">
    <PanelSectionHeader label={t('commandCenter.attention_filter')} count={blockers.length} />
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 items-start" data-testid="home-needs-you-grid">
      {shown.map(item => (
        <div key={item.id} className="min-w-0">
          <AttentionCard item={item} title={title(item.slot)} context={detail(item.slot)}
            onDraftChange={item.question ? answers => data.onQuestionDraftChange(item.question!, answers) : undefined} />
        </div>
      ))}
      {data.restoredQuestionNotices.map(notice => (
        <div key={notice.id} className="min-w-0">
          <RestoredQuestionNotice slot={notice.slot} question={notice.question} title={sessionBySlot.has(notice.slot) ? title(notice.slot) : undefined}
            onDismiss={() => data.dismissRestoredQuestionNotice(notice.id)} />
        </div>
      ))}
    </div>
    {/* A card past the fold is not mounted; the draft state the command center keeps
        for every question restores a half-typed answer when the card appears. */}
    {hidden > 0 && <Btn onClick={() => setVisible(v => v + BLOCKERS_PER_ROW)} data-testid="home-needs-you-more">{t('pages.homePage.show_more_blockers', { count: nextStep, amount: fmtNumber(nextStep) })}</Btn>}
    {empty && <Card><EmptyState icon={<Inbox size={24} />} title={t('commandCenter.no_input')} testId="home-needs-you-empty" /></Card>}
    {suggestions.length > 0 && (
      <Card className="py-1">
        <details data-testid="home-suggestions">
          <summary className="cursor-pointer text-sm text-muted py-1">{t('pages.homePage.suggestions_fold', { count: suggestions.length, amount: fmtNumber(suggestions.length) })}</summary>
          <ul className="list-none m-0 p-0">{suggestions.map(item => <SuggestionRow key={item.id} item={item} title={title(item.slot)} />)}</ul>
        </details>
      </Card>
    )}
  </section>
}
