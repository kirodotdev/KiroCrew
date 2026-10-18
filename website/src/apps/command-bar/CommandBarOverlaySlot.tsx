import type { ReactNode } from 'react'
import {
  ArrowRight,
  Clock,
  Cog,
  Folder,
  Loader2,
  MessageSquare,
  Package,
  RotateCcw,
  Search,
  Send,
  Terminal,
} from 'lucide-react'

import { Highlighted } from '../../components/commandPalette/Highlighted'
import { matesIcon } from './matesProvider'
import { i18nT } from '../../i18n/t'

import type { RootGroup, RowStatus } from './rootIndex'
import { ASK_SLOT_KEY, META_SEP, kindLabel, type Scope, type Slot } from './commandBarOverlayModel'

/**
 * How one list position renders: its icon, title, subtitle, right-hand accessory and
 * arrow, drawn through one shell that every slot tag shares. Hook-free: the overlay
 * builds the renderer on each render, after its last hook, with the live query and
 * the scopes' queries. A new slot tag's look belongs here.
 */

function groupIcon(group: RootGroup) {
  switch (group) {
    case 'attention':
      return <MessageSquare size={14} className="lucide-inline" />
    case 'recent':
      return <MessageSquare size={14} className="lucide-inline" />
    case 'commands':
      return <Terminal size={14} className="lucide-inline" />
    case 'apps':
      return <Package size={14} className="lucide-inline" />
    case 'settings':
      return <Cog size={14} className="lucide-inline" />
  }
}

/**
 * The right-hand column when a row has live state.
 *
 * One renderer for both row kinds -- a launcher row built from the store and a
 * session row built by a view's engine -- because the two would otherwise drift into
 * two treatments of the same fact. `pill` is reserved for what the user OWES the
 * session; a running session gets a dot, so "needs me" and "busy" never look alike.
 *
 * Sizing: only the pill and the dot are unshrinkable, because they are the signal and
 * they are a few pixels wide. Everything textual yields -- the label truncates and
 * the detail is dropped outright below the `sm` breakpoint -- so the ROW TITLE always
 * wins the contest for space. Held the other way round (a rigid accessory) a long
 * tool name collapsed the title on a 320px viewport, which inverts the point: the
 * title is what identifies the row, the detail is a bonus.
 */
function statusAccessory(status: RowStatus) {
  return (
    <span className="flex items-center gap-1.5 text-[11px] min-w-0 overflow-hidden">
      {status.pill ? (
        <span
          className="shrink-0 px-1.5 rounded text-[10px] uppercase tracking-wide"
          style={{ background: `var(${status.colorVar})`, color: 'var(--bg)' }}
        >
          {status.label}
        </span>
      ) : (
        <>
          <span
            className={`shrink-0 w-1.5 h-1.5 rounded-full${status.pulse ? ' animate-pulse' : ''}`}
            style={{ background: `var(${status.colorVar})` }}
          />
          <span className="truncate" style={{ color: `var(${status.colorVar})` }}>
            {status.label}
          </span>
        </>
      )}
      {status.detail && (
        <span className="hidden sm:inline truncate text-muted max-w-[180px]">
          {status.detail}
        </span>
      )}
    </span>
  )
}

/**
 * The overlay's row renderer for this render: `slotParts` and `renderSlot` read the
 * live query, the scope, each view's query and the in-flight row from the argument.
 */
export function commandBarOverlaySlotRenderer({
  query,
  scope,
  artifactsQuery,
  scopedQuery,
  folderQuery,
  mateQuery,
  pendingRow,
}: {
  query: string
  scope: Scope
  artifactsQuery: string
  scopedQuery: string
  folderQuery: string
  mateQuery: string
  pendingRow: string | null
}): (slot: Slot) => ReactNode {
  /**
   * What one slot puts in each of the row's four columns.
   *
   * Split from the shell below so every tag renders through the SAME shell: the
   * right-hand column only shares one edge if one element owns its width, and the
   * previous form (per-branch JSX) is how a `view` row's label ended up pushed left
   * by its own arrow.
   */
  const slotParts = (
    slot: Slot,
  ): { icon: ReactNode; title: ReactNode; subtitle?: ReactNode; accessory?: ReactNode; arrow?: boolean; dim?: boolean; wrap?: boolean } => {
    switch (slot.tag) {
      case 'root': {
        const row = slot.row
        return {
          icon: row.icon ?? groupIcon(row.group),
          title: <Highlighted text={row.title} indices={row.indices} />,
          // Settings titles repeat across tabs ("Speed" exists on more than one), so
          // the row is only identifiable with its subtitle rendered — and when the
          // subtitle is what MATCHED, it is highlighted, because an unhighlighted row
          // in a filtered list reads as a row that should not be there.
          subtitle: row.subtitle
            ? row.matchField === 'subtitle'
              ? <Highlighted text={row.subtitle} indices={row.subtitleIndices ?? []} />
              : row.subtitle
            : undefined,
          accessory: (
            <>
              {/* The alias that put this row here. Without it a keyword hit rendered
                  with no highlight anywhere: typing `theme` listed settings rows whose
                  title and subtitle both lack the word, and the row offered the reader
                  no way to tell why it had matched. */}
              {row.matchField === 'keyword' && row.matchedKeyword && (
                <span className="truncate text-[11px] text-muted max-w-[140px]">
                  {row.matchedKeyword}
                </span>
              )}
              {/* Live state OUTRANKS the static kind label: on the one row where both
                  could apply, what the session is waiting for is the reason the row is
                  on screen and "Session" would be the reason it is not. */}
              {row.status ? (
                statusAccessory(row.status)
              ) : (
                <span className="shrink-0 text-[11px] text-muted">
                  {kindLabel(row, query.trim().length > 0)}
                </span>
              )}
            </>
          ),
          arrow: row.kind === 'view' || row.kind === 'prompt',
        }
      }
      case 'result': {
        const row = slot.row
        // The live state a session row carries, mapped onto the same shape the
        // launcher rows use. This is the surface's own advantage and it was being
        // thrown away: the provider computes an approval pill, a pulsing "Thinking…"
        // and the running tool's name, and the row was rendering a folder and a clock.
        const status: RowStatus | undefined =
          row.statusLabel && row.statusColorVar
            ? {
                colorVar: row.statusColorVar,
                label: row.statusLabel,
                detail: row.statusDetail,
                pulse: row.statusPulse,
                pill: row.statusStyle === 'pill',
              }
            : undefined
        // Where it lives and when it was last touched — the two things that tell two
        // similarly-titled conversations apart. Yields the column to live state,
        // which is the more urgent fact about the same row.
        const meta = [row.folder, row.timestamp].filter(Boolean).join(META_SEP)
        return {
          icon: row.icon,
          title: <Highlighted text={row.title} indices={row.indices} />,
          subtitle: row.subtitle ? (
            <Highlighted text={row.subtitle} indices={row.subtitleIndices ?? []} />
          ) : undefined,
          accessory: status
            ? statusAccessory(status)
            : meta
              ? <span className="truncate text-[11px] text-muted max-w-[180px]">{meta}</span>
              : undefined,
        }
      }
      case 'ask':
        // Every other surface in this product ends in saying something to an agent, so
        // the typed text always has this way out — named with the text itself so the
        // row states what it will send rather than advertising a feature.
        return {
          icon: <Send size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.ask_agent', { query: query.trim() }),
          arrow: true,
          dim: true,
        }
      case 'fallback':
        // The root does not search content, so the typed text still has somewhere to
        // go: one Enter carries it into the sessions view instead of scanning the
        // corpus on every keystroke.
        return {
          icon: <Search size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.fallback_sessions', { query }),
          arrow: true,
          dim: true,
        }
      case 'fallback-artifacts':
        // Named with the query so the row states which text it carries, the same way
        // the sessions row above it does. Both are dim: they are ways out of the
        // root, not results, and a reader scanning for a match should read past them.
        //
        // Its subtitle is its OWN, not the view row's. It needs a gloss at all because
        // "artifact" is our word and a reader who arrives here by typing a name has
        // not necessarily read the view row. But when the typed word also matches the
        // command ("widget", "saved"), both rows are on screen at once, and sharing
        // one subtitle made them read as duplicates -- a reader could not tell how
        // their results would differ. So this one names what it does with the text
        // that is already typed, while the view row still describes the corpus.
        return {
          icon: <Package size={13} className="lucide-inline" />,
          title: i18nT('apps.commandBar.fallback_artifacts', { query }),
          subtitle: i18nT('apps.commandBar.fallback_artifacts_sub'),
          arrow: true,
          dim: true,
        }
      case 'fallback-folders':
        // Named with the query, dim, and arrowed like the two rows above it, for the
        // same reasons. No subtitle: "folder" is the reader's own word for the thing
        // the sidebar already shows them, so there is nothing to gloss.
        return {
          icon: <Folder size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.fallback_folders', { query }),
          arrow: true,
          dim: true,
        }
      case 'fallback-mates':
        // Named with the query, dim and arrowed like the three rows above it. No
        // subtitle, and for a different reason than the folders row: the view row this
        // leads to carries the gloss, and when the typed word matches both ("crew",
        // "agent") the two rows are on screen together — which is exactly where the
        // artifacts pair learned that one shared subtitle makes them read as
        // duplicates. This row names the text it carries; the view row describes the
        // corpus.
        return {
          icon: matesIcon(),
          title: i18nT('apps.commandBar.fallback_mates', { query }),
          arrow: true,
          dim: true,
        }
      case 'recovery':
        // Naming the corpora this surface does NOT reach -- at the moment the user is
        // looking for them -- is what keeps a typed knowledge or skill name from being
        // a silent dead end. Artifacts is no longer among them: it has a view of its
        // own now, so the string stopped listing it.
        //
        // The second clause names what CLICKING does, not what the user must switch
        // off, and names it without implying a transaction. Three readers in sequence
        // failed it. "disable Command Bar" described the end state, so the row itself
        // read as the thing that would disable their search box. Naming the App Store
        // fixed that and introduced a cost: "App Store sounds like it might want me to
        // install or buy something." The destination is neither -- it is AppDetailPage
        // at `/apps/detail/command-bar`, this app's OWN page under Apps, carrying its
        // Enabled switch. So the row names that section. Wording that blocks the users
        // a row exists for is not a smaller bug than a broken link.
        //
        // What is NOT solvable here: the same reader did not know what "Command Bar"
        // is. They are inside it. A row cannot teach the name of the app it lives in,
        // and the instruction is unactionable without naming the app to turn off.
        //
        // This is the one row that WRAPS. Every other row is a short label, but this
        // one is a sentence, and a sentence truncated mid-clause loses exactly the
        // half that explains itself, so the row is allowed two lines to finish. The
        // capture harness measures that: it asserts this row renders with no ellipsis
        // and that the clamp still engages when the text is longer than two lines.
        return {
          icon: <Package size={13} className="lucide-inline" />,
          title: i18nT('apps.commandBar.other_search_hint'),
          arrow: true,
          dim: true,
          wrap: true,
        }
      case 'retry-artifacts':
        // Separate tags keep the sessions row byte-identical to main, so this change
        // does not reshape a surface it does not own.
        return {
          icon: <RotateCcw size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.retry'),
        }
      case 'retry-folders':
        // Plain Retry, like the artifacts row above: WHAT failed is said by the
        // `ErrorNotice` above the list, which is where an error surfaced to the user
        // belongs (AUTOSDE `errors-use-error-notice`). Its own tag keeps the sessions
        // row below byte-identical to main, whose hand-written text this change does
        // not own and does not touch.
        return {
          icon: <RotateCcw size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.retry'),
        }
      case 'retry-mates':
        // Plain Retry, like the two rows above: WHAT failed is said by the
        // `ErrorNotice` over the list, which is where an error surfaced to the user
        // belongs (AUTOSDE `errors-use-error-notice`).
        return {
          icon: <RotateCcw size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.retry'),
        }
      case 'retry':
        return {
          icon: <RotateCcw size={14} className="lucide-inline" />,
          title: <span className="text-danger">{i18nT('apps.commandBar.search_failed')}</span>,
        }
      case 'clear-query':
        // One row carrying both halves: the search found nothing, and the listing is
        // one Enter away. Split across a message and a control they were two things
        // to read; as a row it is one thing to do.
        return {
          icon: <Clock size={14} className="lucide-inline" />,
          title:
            scope === 'artifacts'
              ? i18nT('apps.commandBar.no_artifact_match_show_recent', { query: artifactsQuery })
              : i18nT('apps.commandBar.no_match_show_recent', { query: scopedQuery }),
          dim: true,
        }
      case 'clear-query-folders':
        // The same row for the folders view, with the folder glyph rather than the
        // clock: what it returns to is the whole list in sidebar order, which has no
        // recency for a clock to stand for.
        return {
          icon: <Folder size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.no_match_show_all_folders', { query: folderQuery }),
          dim: true,
        }
      case 'clear-query-mates':
        // The same row again, with the crew glyph: what it returns to is the whole
        // roster, which has no recency for a clock to stand for either.
        return {
          icon: matesIcon(),
          title: i18nT('apps.commandBar.no_match_show_all_mates', { query: mateQuery }),
          dim: true,
        }
      case 'open-crewmates':
        // Both halves in one row: there is no crew, and Enter goes where one is made.
        // Arrowed like every other row that leaves this surface, and WRAPPED, because
        // this one is a sentence and a sentence truncated mid-clause loses the half
        // that explains it.
        // Its own string, ending in what ENTER does -- the same shape
        // `no_match_show_all_mates` uses. The notice copy it replaced named an errand
        // somewhere else ("Add one on the Crewmates page") and said nothing about this
        // keystroke, so a crew-less reader -- the only audience this state has -- read it
        // as text and went hunting by hand.
        return {
          icon: matesIcon(),
          title: i18nT('apps.commandBar.no_mates_yet_open'),
          arrow: true,
          dim: true,
          wrap: true,
        }
    }
  }

  const renderSlot = (slot: Slot): ReactNode => {
    const parts = slotParts(slot)
    return (
      <>
        <span className="shrink-0 w-4 flex justify-center text-muted">{parts.icon}</span>
        <span className="flex-1 min-w-0">
          <span className={`block ${parts.wrap ? 'line-clamp-2' : 'truncate'}${parts.dim ? ' text-muted' : ''}`}>{parts.title}</span>
          {parts.subtitle && (
            <span className="block truncate text-[11px] text-muted">{parts.subtitle}</span>
          )}
        </span>
        {parts.accessory}
        {/* The arrow gets a slot of its own on EVERY row, not just the rows that have
            one. Rendered inline it pushed the label of a `view` row left by its own
            width, so the labels stopped sharing a right edge and the column read as
            misaligned. */}
        <span className="shrink-0 w-[13px] flex justify-end">
          {parts.arrow && <ArrowRight size={13} className="lucide-inline text-muted" />}
        </span>
        {/* The row doing awaited work says so. Both kinds that can be in flight are
            named here: an `invoke` launcher row, and the ask row. */}
        {((slot.tag === 'root' && pendingRow === slot.row.id) ||
          (slot.tag === 'ask' && pendingRow === ASK_SLOT_KEY)) && (
          <Loader2
            size={13}
            aria-label={i18nT('apps.commandBar.working')}
            className="lucide-inline text-muted shrink-0 animate-spin"
          />
        )}
      </>
    )
  }

  return renderSlot
}
