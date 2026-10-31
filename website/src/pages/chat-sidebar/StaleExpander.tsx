/** The "Show N sleeping sessions" expander a lane draws above a container's
 *  dormant rows. Its own component because the explanation is an instant tip,
 *  and `useInstantTip` is a hook that the lane's render helpers cannot call. */
import { useId } from 'react'
import { InstantTip, useInstantTip } from '../../components/InstantTip'
import { i18nT } from '../../i18n/t'
import { formatRecentWindow } from '../recentWindow'
import { DisclosureChevron, STALE_TOGGLE_CLS } from './chrome'

export function StaleExpander({ containerId, containerName, count, open, collapseMs, regionId, onToggle }: {
  containerId: string
  containerName?: string
  count: number
  open: boolean
  collapseMs: number
  regionId: string
  onToggle: () => void
}) {
  const lblId = `stale-lbl-${containerId}`
  const ctxId = `stale-ctx-${containerId}`
  // One pluralised sentence carries the count AND says where the rows went,
  // so a folder badge of "2" over one visible row plus "1 dormant session
  // hidden" visibly adds up. A bare noun + count pill read as a category,
  // not as "the rest are in here" (gui-user-test friction on this row).
  const label = open
    ? i18nT('pages.chatSidebar.stale_collapse_row_shown', { count })
    : i18nT('pages.chatSidebar.stale_collapse_row_hidden', { count })
  // "Sleeping" is the row's word for dormant, and a first-time user has no
  // other place to learn it: the tip says what it means (still open, only
  // unused past the threshold), that nothing was closed, and which menu row
  // changes it. The threshold uses the same compact label ("7d") as that menu.
  const vars = {
    window: formatRecentWindow(collapseMs),
    setting: i18nT('pages.chatSidebar.stale_collapse_menu'),
    menu: i18nT('pages.chatSidebar.sort_filter_sessions'),
  }
  const hint = open
    ? i18nT('pages.chatSidebar.stale_collapse_row_hint_open', vars)
    : i18nT('pages.chatSidebar.stale_collapse_row_hint', vars)
  const { tip, tipHandlers, tipId } = useInstantTip()
  // The bubble mounts only while open, so the button is described by a copy
  // that is always in the DOM (`HoverTip` does the same). `hidden`, not
  // `sr-only`: an sr-only span would also be read inline as button text.
  const descId = useId()
  const { 'aria-describedby': _bubbleId, ...handlers } = tipHandlers
  void _bubbleId
  return (
    <>
      {/* aria-labelledby composes the visible sentence + a visually-hidden
          container name, so AT announces "Show 1 sleeping session <folder>"
          — an aria-label would override the button contents and re-composing
          it per locale is exactly the concatenation trap the i18n rules ban. */}
      <button type="button"
        aria-expanded={open}
        aria-controls={regionId}
        aria-labelledby={`${lblId} ${ctxId}`}
        aria-describedby={descId}
        data-testid={`stale-expander-${containerId}`}
        onClick={onToggle}
        {...handlers}
        data-stale-toggle="" className={STALE_TOGGLE_CLS}>
        <DisclosureChevron open={open} size={12} />
        <span id={lblId} className="tabular-nums">{label}</span>
        <span id={ctxId} className="sr-only">{containerName
          ? i18nT('pages.chatSidebar.stale_collapse_ctx_in_name', { name: containerName })
          : i18nT('pages.chatSidebar.stale_collapse_row_ungrouped')}</span>
      </button>
      <span id={descId} hidden data-testid={`stale-expander-hint-${containerId}`}>{hint}</span>
      <InstantTip tip={tip} tipId={tipId} className="w-max max-w-[min(20rem,calc(100vw-1rem))] whitespace-normal break-words">
        {hint}
      </InstantTip>
    </>
  )
}
