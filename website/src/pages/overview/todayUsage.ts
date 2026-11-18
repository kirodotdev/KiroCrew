/**
 * Today's usage, read once for every surface that shows it (Settings > Overview's
 * Usage card, Home's Today tile). One hook, so the two cannot disagree on which
 * day is "today" or on when tokens and cost are shown.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { reportsDollars, type AcpBackendConfig } from '../../api/acpBackend'
import { providerUsageQuery } from '../../api/providerUsageQuery'
import { failedWithNoData } from '../../api/queryState'
import { compactPluralCount, fmtCompact } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useProvider } from '../../providers'
import { formatCost } from '../../utils/formatCost'

/** A Today-line count: `amount` is the locale's compact number, and `count`, the
 *  quantity it shows, picks the plural form that agrees with it. */
function todayCount(n: number | undefined | null): { count: number; amount: string } {
  return { count: compactPluralCount(n ?? 0), amount: fmtCompact(n ?? Number.NaN) }
}

/** The Today line: sessions and messages, then tokens and cost when known. */
function todayLine(sessions: number | undefined, messages: number | undefined, tokens: number | null | undefined, cost: number | null | undefined): string {
  const parts = [
    i18nT('pages.overviewPage.today_sessions', todayCount(sessions)),
    i18nT('pages.overviewPage.today_messages', todayCount(messages)),
  ]
  if (tokens != null) parts.push(i18nT('pages.overviewPage.today_tokens', todayCount(tokens)))
  if (cost != null) parts.push(formatCost(cost))
  return i18nT('pages.overviewPage.today_line', { summary: parts.join(' · ') })
}

/**
 * The provider's usage report with today's figures picked out.
 *
 * Today's tokens and cost come from the bucket of the gateway's own local day
 * (the token history's totals cover 30 days, and the browser may sit in another
 * zone), and only on a harness that bills in dollars: a credit-billed install's
 * Today line stays sessions and messages. With no config ever read, the harness
 * is unknown (`harnessUnread`) and the line drops its tokens and cost.
 */
export function useTodayUsage() {
  const provider = useProvider()
  const usage = useQuery(providerUsageQuery(provider))
  const cfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const data = usage.data
  const today = data?.sessions.today
  // The gateway's own local day's token bucket, whatever the harness bills in;
  // the Today LINE shows its tokens and cost only on a dollar-billed harness.
  const todayBucket = data?.localDay ? data.tokenDailyHistory?.find(d => d.date === data.localDay) : undefined
  const todayTokens = reportsDollars(cfgQuery.data) ? todayBucket : undefined
  const tokens = todayTokens && todayTokens.input + todayTokens.output + todayTokens.cacheCreate + todayTokens.cacheRead
  return {
    provider,
    usage,
    cfgQuery,
    harnessUnread: failedWithNoData(cfgQuery),
    today,
    todayBucket,
    costUsd: todayTokens?.costUsd,
    /** The one-line summary, or undefined until the usage report has answered. */
    line: data ? todayLine(today?.sessions, today?.messages, tokens, todayTokens?.costUsd) : undefined,
  }
}
