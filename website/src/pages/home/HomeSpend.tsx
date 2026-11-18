/**
 * Home's "Tokens spent": today's usage split by model as one stacked bar
 * and a row per model, from the same usage report the Today tile reads. On a
 * dollar-billed harness the split is cost; on a credit-billed one it is tokens,
 * which every harness reports. Drawn only when the provider tracks usage.
 */
import { useTranslation } from 'react-i18next'
import { Card, EmptyState, PanelSectionHeader } from '../../components/ui'
import { fmtCompact, fmtNumber, fmtPercentFloor } from '../../i18n/format'
import { formatCost } from '../../utils/formatCost'
import { useTodayUsage } from '../overview/todayUsage'
import { spendByModel } from './homeModel'
import { RowLink } from './homeRows'

/** Theme tints for the bar's segments, largest share first; theme tokens only. */
const SEGMENT_CLASSES = ['bg-accent', 'bg-accent/70', 'bg-accent/45', 'bg-accent/25', 'bg-border-strong']

export default function HomeSpend({ dollars }: { dollars: boolean }) {
  const { t } = useTranslation()
  const usage = useTodayUsage()
  if (!usage.provider.capabilities.usageBilling) return null
  const rows = spendByModel(usage.todayBucket?.models, dollars, SEGMENT_CLASSES.length, t('pages.homePage.spend_other'))
  const total = rows.reduce((sum, r) => sum + r.amount, 0)
  const amount = (n: number) => dollars ? formatCost(n) : t('pages.overviewPage.today_tokens', { count: n, amount: fmtCompact(n) })
  const plan = usage.usage.data?.billing
  return <section aria-label={t('pages.homePage.spend_title')} className="space-y-3" data-testid="home-spend">
    <PanelSectionHeader label={t('pages.homePage.spend_title')} count={usage.usage.data ? amount(total) : undefined}
      trailing={<RowLink to="/settings/overview?view=usage">{t('pages.homePage.open_page')}</RowLink>} />
    <Card className="space-y-3">
      {usage.usage.data && !rows.length && <EmptyState icon={null} title={t('pages.homePage.spend_none')} testId="home-spend-empty" />}
      {rows.length > 0 && <>
        <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-bg-elevated" role="img" aria-label={t('pages.homePage.spend_bar_label')} data-testid="home-spend-bar">
          {rows.map((r, i) => <span key={r.model} className={SEGMENT_CLASSES[i]} style={{ width: `${(r.amount / total) * 100}%` }} data-testid="home-spend-segment" />)}
        </div>
        <ul className="list-none m-0 p-0 space-y-1.5">
          {rows.map((r, i) => (
            <li key={r.model} className="flex items-center gap-2 min-w-0 text-[13px]" data-testid="home-spend-row">
              <span className={`w-2 h-2 rounded-full shrink-0 ${SEGMENT_CLASSES[i]}`} />
              <span className="flex-1 min-w-0 truncate">{r.model}</span>
              <span className="text-muted tabular-nums shrink-0">{fmtPercentFloor(r.amount / total, 0)}</span>
              <span className="tabular-nums shrink-0 w-20 text-right">{amount(r.amount)}</span>
            </li>
          ))}
        </ul>
      </>}
      {plan?.plan && typeof plan.percentUsed === 'number' && (
        <div className="text-[12px] text-muted">{t('pages.homePage.spend_plan', { plan: plan.plan, percent: fmtPercentFloor(plan.percentUsed / 100, 0), amount: fmtNumber(plan.percentUsed) })}</div>
      )}
    </Card>
  </section>
}
