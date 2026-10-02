import { useId, type ReactElement } from 'react'
import { useTranslation } from 'react-i18next'
import { PanelRightOpen } from 'lucide-react'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { fmtNumber } from '../../../i18n/format'
import { approvalTitle, questionText, runTitle, type AttentionItem, type CommandCenterModel, type RunNode } from './model'
import type { Tile } from './StatusTiles'

/** Rows one list shows before the rest is left to the side panel, whose
 * Overview is the agent's published page (one line per work item is the
 * layout it is asked for). */
const MAX_ROWS = 6
export const TILE_LABEL_KEYS: Record<Tile, string> = { progress: 'commandCenter.tile_progress', blocked: 'commandCenter.blocked', attention: 'commandCenter.attention_filter' }

type Data = Pick<CommandCenterModel, 'nodes' | 'workItems' | 'attention' | 'progress'>

/** The short list behind one tile: what runs, what is stuck, or what waits on
 * the user. A run's error renders through the shared error notice. A row only
 * names its item and mounts no answer or approval control, so a draft has one
 * home: the panel. With `onOpen` (the dock) the row itself is the hand-off, a
 * full-width button named by the item's title that opens the panel, the same
 * place the dock's labelled Open Dashboard button and the overflow row reach;
 * without it the row is plain text. */
export default function TileList({ tile, data, onOpen }: { tile: Tile; data: Data; onOpen?: () => void }) {
  const { t } = useTranslation()
  const idBase = useId()
  // `title` is the row's accessible name; a leading `badge` (the request kind)
  // and the trailing `detail` are its description, so a reader hears the item
  // first and its state after.
  const row = (key: string, title: string, detail?: string, badge?: string, error?: string) => {
    const titleId = `${idBase}-${key}-title`
    const badgeId = `${idBase}-${key}-badge`
    const detailId = `${idBase}-${key}-detail`
    const describedBy = [badge && badgeId, detail && detailId].filter(Boolean).join(' ') || undefined
    const content = <>
      {badge && <span id={badgeId} className="shrink-0 rounded-full bg-danger-subtle text-danger px-1.5 py-px text-[11px] font-medium">{badge}</span>}
      <span id={titleId} className="truncate min-w-0">{title}</span>
      {detail && <span id={detailId} className="truncate text-muted min-w-0 flex-1">{detail}</span>}
    </>
    return <li key={key} className="min-w-0 text-[12px] space-y-1">
      {onOpen
        // The row's imperative title ("Open the pull request") could read as
        // the action itself; the trailing glyph is the dock's own Open
        // Dashboard icon, so the click reads as "show me this in the panel".
        // Decorative: the accessible name stays the item's title.
        ? <Btn type="button" onClick={onOpen} aria-labelledby={titleId} aria-describedby={describedBy}
          className="flex w-full min-w-0 items-center gap-2 rounded-sm border-0 px-1 py-0.5 text-left text-[12px]">
          {content}
          <PanelRightOpen size={12} aria-hidden="true" className="text-muted shrink-0 ml-auto" />
        </Btn>
        : <div className="flex items-center gap-2 min-w-0">{content}</div>}
      {/* No hand-off: the composer and panel beside this list can hold unsent answer drafts. */}
      {error && <ErrorNotice message={error} />}
    </li>
  }
  const nodeRow = (node: RunNode) => row(node.id, runTitle(node), node.detail, undefined, node.error)
  const workRow = (w: Data['workItems'][number]) => row(w.item_id, w.title, w.summary)
  let rows: ReactElement[] = []
  let lead: ReactElement | null = null
  if (tile === 'progress') {
    // The list reads exactly the source selected for the tile's number by
    // `progress.source`; without work progress, it shows what is running now.
    // Only running items: a blocked item is the Blocked tile's row, and a
    // waiting item is not progress until its question is answered, so no row
    // appears under two tiles and none claims motion it lacks.
    rows = data.progress?.source === 'work'
      ? data.workItems.filter(w => w.state === 'running').map(workRow)
      : data.nodes.filter(n => n.state === 'running').map(nodeRow)
    if (!rows.length) rows = [<li key="empty" className="text-[12px] text-muted">{t('commandCenter.nothing_running')}</li>]
    // The lead follows the same discriminator as the rows: whenever the list is
    // runs rather than board items, the dock's disclosed list opens by saying
    // why. Without a plan the tile's number is the running count (the same test
    // StatusTiles applies), so the lead says the runs are what is counted; with
    // a TODO plan the number is the plan's and the rows are still the runs, so
    // the lead names both and keeps the two from reading as one readout. The
    // tile's title says the same on hover, and the panel's info control already
    // explains its own readout. The sentence sits outside the capped rows, so it
    // takes no row from them.
    if (onOpen && !data.progress) lead = <li className="text-[12px] text-muted">{t('commandCenter.running_title')}</li>
    else if (onOpen && data.progress?.source === 'todo') lead = <li className="text-[12px] text-muted">{t('commandCenter.plan_runs_title')}</li>
  } else if (tile === 'blocked') {
    rows = [
      ...data.nodes.filter(n => n.state === 'blocked').map(nodeRow),
      ...data.workItems.filter(w => w.state === 'blocked').map(workRow),
    ]
    if (!rows.length) rows = [<li key="empty" className="text-[12px] text-muted">{t('commandCenter.no_blocked')}</li>]
  } else {
    rows = data.attention.map((item: AttentionItem) => {
      const node = data.nodes.find(n => n.id === `session:${item.slot}`)
      // One row is one request, so its badge is singular: the plural tab names
      // (Approvals, Questions) stay with the panel's tabs.
      const kind = item.kind === 'approval' ? t('commandCenter.badge_approval') : item.kind === 'question' ? t('commandCenter.badge_question') : t('commandCenter.state_needs_input')
      // A follow-up ask has no wording of its own, so its row leads with the
      // session it belongs to; two waiting sessions must read as two rows.
      const followUp = !!item.question?.followUp && !!node
      const title = item.approval ? approvalTitle(item.approval) || t('commandCenter.approval_needed')
        : followUp ? runTitle(node!) : (item.question && questionText(item.question)) || (node ? runTitle(node) : '')
      return row(item.id, title, followUp ? questionText(item.question!) : undefined, kind)
    })
    if (!rows.length) rows = [<li key="empty" className="text-[12px] text-muted">{t('commandCenter.no_input')}</li>]
  }
  return <ul className="list-none m-0 p-0 space-y-1">
    {lead}
    {rows.slice(0, onOpen ? MAX_ROWS : rows.length)}
    {onOpen && rows.length > MAX_ROWS && <li><Btn onClick={onOpen} className="px-2 py-0.5 text-[12px]">{t('commandCenter.more_in_panel', { countText: fmtNumber(rows.length - MAX_ROWS) })}</Btn></li>}
  </ul>
}
