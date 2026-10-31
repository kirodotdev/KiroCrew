import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { motion, useReducedMotion } from 'framer-motion'
import { CalendarCheck, Check, Circle, MessageSquare, X, type LucideIcon } from 'lucide-react'
import { OverlayPortal, useCloseWhenCrewHidden } from '../overlayRoot'
import Glass from '../Glass'
import { Btn, IconButton } from '../ui'
import { Progress } from '../ui/progress'
import ErrorNotice from '../ErrorNotice'
import { useIsMobile } from '../../hooks/useIsMobile'
import { i18nT } from '../../i18n/t'
import { fmtDate, fmtNumber } from '../../i18n/format'
import { errMessage } from '../../utils/thunkError'
import type { MilestoneFeatureItem, MilestoneUsageItem } from '../../api/client/featureDiscovery'
import { FEATURE_COPY, GETTING_STARTED_TITLE, USAGE_COPY } from './copy'
import ExploreIntroVideo from './ExploreIntroVideo'
import {
  closeMilestonesPanel, earnedBadges, featureItems, gettingStarted, milestonesLoadFailed, refreshMilestones, tryToday, useMilestoneActions,
  useMilestones, useMilestonesPanel, useRunStarter, usageItem, type EarnedBadge,
} from './useMilestones'

const CARD = 'notif-material glass-shadow block rounded-2xl px-3 py-2.5'

/** Line icons per badge row. No trophies, no medals. */
const BADGE_ICON: Readonly<Record<string, LucideIcon>> = {
  'chats-opened': MessageSquare,
  'streak-best': CalendarCheck,
}

/** The badge wall: one quiet badge per finished tier, colored for an advanced one. No motion. */
function BadgeWall({ badges }: { badges: EarnedBadge[] }) {
  if (badges.length === 0) return null
  return (
    <div data-testid="milestones-badges">
      <div className="text-[12px] text-muted mb-1.5">{i18nT('milestones.badges')}</div>
      <ul className="list-none m-0 p-0 flex flex-wrap gap-1.5">
        {badges.map(b => {
          const Icon = BADGE_ICON[b.rowId] ?? MessageSquare
          const label = i18nT(USAGE_COPY[b.rowId].tier, { count: b.tier })
          return (
            <li
              key={`${b.rowId}:${b.tier}`}
              data-testid={`milestone-badge-${b.rowId}-${b.tier}`}
              data-kind={b.kind}
              title={fmtDate(b.at * 1000)}
              // Every badge here is earned, so none may look locked: all share the
              // earned border and text. An advanced one adds the colored fill.
              className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-lg border border-accent/40 text-[12px] tabular-nums ${b.kind === 'advanced' ? 'bg-accent-subtle text-accent' : 'text-text'}`}
            >
              <Icon size={13} strokeWidth={1.75} className={b.kind === 'advanced' ? 'text-accent' : 'text-text'} aria-hidden="true" />
              <span>{label}</span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** Scroll a row into view when a notice opened the panel at it. */
function useScrollWhenFocused<T extends HTMLElement>(focused: boolean) {
  const ref = useRef<T>(null)
  useEffect(() => { if (focused) ref.current?.scrollIntoView({ block: 'nearest' }) }, [focused])
  return ref
}

/** An achievement: its value against the next tier, as a progress bar, using
 *  the server's thresholds. Past the last tier the bar stays full. No nudging text. */
function AchievementRow({ item, focused = false }: { item: MilestoneUsageItem; focused?: boolean }) {
  const copy = USAGE_COPY[item.id]
  const ref = useScrollWhenFocused<HTMLDivElement>(focused)
  if (!copy) return null
  const next = item.tiers.find(t => t > item.value) ?? item.tiers[item.tiers.length - 1]
  const pct = next > 0 ? Math.min(100, (item.value / next) * 100) : 0
  const title = i18nT(copy.title)
  return (
    <div ref={ref} data-testid={`milestone-usage-${item.id}`}>
      <div className="flex items-baseline justify-between gap-2 text-[13px]">
        <span className="text-text">{title}</span>
        <span className="tabular-nums text-muted" data-testid={`milestone-next-${item.id}`}>{fmtNumber(item.value)} / {fmtNumber(next)}</span>
      </div>
      <Progress value={pct} className="mt-1.5" aria-label={title} />
    </div>
  )
}

function FeatureRow({ item, focused, onError, title }: { item: MilestoneFeatureItem; focused: boolean; onError: (m: string) => void; title?: string }) {
  const copy = FEATURE_COPY[item.id]
  const runStarter = useRunStarter()
  const { markDone } = useMilestoneActions()
  const ref = useScrollWhenFocused<HTMLLIElement>(focused)
  // A done row (only in Getting started) keeps its place with a check and no buttons.
  const done = item.state === 'done'
  return (
    <li ref={ref} data-testid={`milestone-row-${item.id}`} data-state={item.state} className={`flex items-start gap-2.5 py-2 ${focused ? 'rounded-lg bg-bg-hover px-1.5 -mx-1.5' : ''}`}>
      {done
        ? <Check size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden="true" />
        : <Circle size={16} className="mt-0.5 shrink-0 text-muted" aria-hidden="true" />}
      <div className="flex-1 min-w-0">
        {/* A row still to try is named by what to do; the past-tense title is for notices. */}
        <div className="text-[13px] text-text">{title ?? i18nT(copy.action)}</div>
        <div className="text-[12px] text-muted">{i18nT(copy.blurb)}</div>
      </div>
      {!done && <div className="shrink-0 flex items-center gap-2">
        <button type="button" className="text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer" onClick={() => { markDone(item.id).catch(e => onError(errMessage(e))) }}>
          {i18nT('milestones.mark_as_done')}
        </button>
        {item.starter && (
          <Btn className="text-[12px] px-2 py-0.5" onClick={() => { onError(''); runStarter(item).then(closeMilestonesPanel, e => onError(errMessage(e) || i18nT('pages.chatPage.unknown_error'))) }}>
            {i18nT('milestones.try_it')}
          </Btn>
        )}
      </div>}
    </li>
  )
}

/**
 * Explore. Shaped like the notification sheet: a column of floating cards that
 * slides in from the right edge under the top bar, full width on a phone.
 * Top to bottom: the "Kiro Crew in 15 seconds" card, "Try today" (up to three
 * untried features, `tryToday`), and "Your progress": the Getting started pair
 * in the first days, the streak, a bar per achievement toward its next tier,
 * and the badge wall. Fixed layout and i18n text only.
 */
export default function MilestonesSheet() {
  const { open, focusId } = useMilestonesPanel()
  const isMobile = useIsMobile()
  const reduceMotion = useReducedMotion()
  const queryClient = useQueryClient()
  const { data, isError, error: loadError } = useMilestones()
  const [error, setError] = useState('')
  // The load failure has no state of its own to clear, so dismissing it is
  // remembered here until Explore opens again.
  const [loadDismissed, setLoadDismissed] = useState(false)
  const sheetRef = useRef<HTMLDivElement>(null)
  const location = useLocation()
  const lastPath = useRef(location.pathname)

  useCloseWhenCrewHidden(open, closeMilestonesPanel)
  useEffect(() => {
    if (open) refreshMilestones(queryClient)
    else { setError(''); setLoadDismissed(false) }
  }, [open, queryClient])
  useEffect(() => {
    if (location.pathname !== lastPath.current) { lastPath.current = location.pathname; closeMilestonesPanel() }
  }, [location.pathname])
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      const t = e.target as Element | null
      if (!t || sheetRef.current?.contains(t) || t.closest('[data-milestones-trigger]')) return
      closeMilestonesPanel()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeMilestonesPanel() }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('pointerdown', onDown); document.removeEventListener('keydown', onKey) }
  }, [open])

  const loadFailed = milestonesLoadFailed(isError, loadError)
  if (!open || (!data?.enabled && !loadFailed)) return null
  const today = tryToday(data)
  const starting = gettingStarted(data)
  // The finished feature row a notice opened Explore at, when no section lists it.
  const listed = new Set([...today, ...starting].map(item => item.id))
  const reached = focusId && !listed.has(focusId)
    ? featureItems(data).find(item => item.id === focusId && item.state === 'done') ?? null
    : null
  const streakItem = usageItem(data, 'streak')
  const sessions = usageItem(data, 'sessions')
  const streak = data?.streak ?? { current: 0, best: 0 }

  return (
    <OverlayPortal>
      <div className={`fixed z-[60] pointer-events-none top-safe-offset-[48px] bottom-safe ${isMobile ? 'left-safe right-safe' : 'right-safe left-safe-or-3'}`}>
        <motion.div
          ref={sheetRef}
          role="dialog"
          aria-label={i18nT('milestones.title')}
          data-testid="milestones-sheet"
          initial={reduceMotion ? false : { x: 24, opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          transition={{ duration: reduceMotion ? 0 : 0.2, ease: 'easeOut' }}
          className={`absolute top-0 bottom-0 right-0 pointer-events-auto ${isMobile ? 'w-full bg-bg' : 'w-[400px]'} overflow-y-auto px-3 py-2 flex flex-col gap-2`}
        >
          <Glass variant="panel" radius={16} className={CARD}>
            <div className="flex items-center gap-2">
              <span className="flex-1 text-[14px] font-bold text-text-strong">{i18nT('milestones.title')}</span>
              <IconButton aria-label={i18nT('milestones.close')} onClick={closeMilestonesPanel}><X size={14} /></IconButton>
            </div>
          </Glass>
          {/* The panel holds no draft, so handing the error to the agent loses nothing. */}
          <ErrorNotice
            message={error || (loadFailed && !loadDismissed ? i18nT('milestones.load_failed') : null)}
            variant="inline"
            askAgent
            onDismiss={() => { if (error) setError(''); else setLoadDismissed(true) }}
          />

          {data?.enabled && (<>
          <ExploreIntroVideo className={CARD} />
          {/* A notice for a finished feature opens here at that row: read-only,
              with its check, and only while it is the row asked for, so Try
              today never takes a finished feature back. */}
          {reached && (
            <Glass variant="panel" radius={16} className={CARD}>
              <ul className="list-none m-0 p-0" data-testid="milestones-reached">
                <FeatureRow item={reached} title={i18nT(FEATURE_COPY[reached.id].title)} focused onError={setError} />
              </ul>
            </Glass>
          )}
          <Glass variant="panel" radius={16} className={CARD}>
            <div className="text-[12px] font-semibold text-muted">{i18nT('milestones.try_today')}</div>
            {today.length > 0 ? (
              <ul className="list-none m-0 p-0" data-testid="milestones-try-today">
                {today.map(item => <FeatureRow key={item.id} item={item} focused={focusId === item.id} onError={setError} />)}
              </ul>
            ) : starting.some(item => item.state === 'open') ? (
              // Only the Getting started pair is left: it is below, so this says nothing false.
              <div className="text-[13px] text-muted mt-1" data-testid="milestones-try-today-none">{i18nT('milestones.try_today_none')}</div>
            ) : (
              <div className="text-[13px] text-muted mt-1" data-testid="milestones-all-tried">{i18nT('milestones.all_tried')}</div>
            )}
          </Glass>

          <Glass variant="panel" radius={16} className={`${CARD} flex flex-col gap-3`}>
            <div className="text-[12px] font-semibold text-muted">{i18nT('milestones.your_progress')}</div>
            {starting.length > 0 && (
              <div data-testid="milestones-getting-started">
                <div className="text-[12px] text-muted">{i18nT('milestones.getting_started')}</div>
                <ul className="list-none m-0 p-0">
                  {starting.map(item => (
                    <FeatureRow key={item.id} item={item} title={i18nT(GETTING_STARTED_TITLE[item.id])} focused={focusId === item.id} onError={setError} />
                  ))}
                </ul>
              </div>
            )}
            <div className="flex items-baseline justify-between gap-2 text-[13px]">
              <span className="text-text">{i18nT('milestones.group_streak')}</span>
              <span className="tabular-nums text-muted" data-testid="milestones-streak-line">
                {i18nT('milestones.streak_days', { count: streak.current, best: streak.best })}
              </span>
            </div>
            {sessions && <AchievementRow item={sessions} focused={focusId === sessions.id} />}
            {streakItem && <AchievementRow item={streakItem} focused={focusId === streakItem.id} />}
            <BadgeWall badges={earnedBadges(data)} />
          </Glass>
          </>)}
        </motion.div>
      </div>
    </OverlayPortal>
  )
}
