/** The Older Sessions pane: the splitter or divider, the footer that opens it
 *  and clears it, and the pane's search, date-segmented or folder-grouped list
 *  and load-more row. */
import { Fragment } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X, Monitor, EyeOff, VenetianMask, MessageSquare, Folder, Clock } from 'lucide-react'
import FolderGlyph from '../../components/FolderGlyph'
import InfoTip from '../../components/InfoTip'
import { offlineProps } from '../../utils/offline'
import { fetchHistory, resumeFromHistory } from '../../store/chatSlice'
import { SEARCH_MIN_CHARS } from '../../api/client'
import { groupHistoryByFolder } from '../../utils/groupHistoryByFolder'
import { slotChannelLabel, slotChannelNamespace } from '../../utils/channelOrigin'
import { SearchInput } from '../../components/ui'
import { ChannelBrandIcon } from '../../components/ChannelBrandIcon'
import { RemoteCrewChip } from '../../components/RemoteCrewChip'
import { compareBySort, fmtRelativeTime } from '../chat/sessionOrder'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import { agentOrDefaultLabel } from '../../utils/agentLabel'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { historyRowIdentity } from './rowIdentity'
import { DisclosureChevron, dateSegment } from './chrome'
import type { SidebarScope } from './scope'

/** The splitter, the footer and the pane, as the sidebar root draws them. */
export function renderHistoryPane(shell: SidebarScope): React.ReactNode {
  const { clearClosedSessionsWithConfirm, collapsedHistoryGroups, connected, defaultAgent,
    deleteHistorySessionWithConfirm, dispatch, folderSortMode, folders, history, historyDragging,
    historyFilter, historyHasMore, historyHeight, historyOpen, historyResize, historySearchResults,
    openHistoryPane, selectInstance, setCollapsedHistoryGroups, setHistoryFilter, setHistoryOpen, sortKey: listSortKey,
    tintAgents } = shell
  // Older sessions have no place in the manual order (it orders the open ones),
  // so under Manual this pane keeps its own newest-first order and date groups.
  const sortKey = listSortKey === 'manual' ? 'date-desc' : listSortKey
  return (
    <>
      {historyOpen ? (
        // Separator that doubles as a Pointer-Events resize handle (drag,
        // mouse/touch/pen) / collapse (double-click); neither gesture is driven
        // from the keyboard on this element.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- the handler the rule sees is onDoubleClick, and the collapse it performs is duplicated on the "Older Sessions" row below (role=button/tabIndex=0, Enter+Space), so the COLLAPSE is keyboard-reachable. The RESIZE is not: usePointerDrag exposes pointer handlers only and this pane has no arrow-key resize anywhere. Giving it one is the ARIA window-splitter keyboard contract — a feature, not a lint fix
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label={i18nT('pages.chatSidebar.resize_history_pane')}
          {...historyResize}
          onDoubleClick={() => setHistoryOpen(false)}
          className="relative h-[6px] cursor-ns-resize z-10 group/drag flex items-center justify-center select-none"
          style={{ touchAction: 'none' }}
        >
          <div className={`w-full transition-all duration-200 ${historyDragging ? 'h-[2px] bg-accent-hover' : 'h-px bg-border group-hover/drag:h-[2px] group-hover/drag:bg-accent'}`} />
        </div>
      ) : (
        <div className="border-t border-border" />
      )}
      {/* Older Sessions footer — the persistent collapse/expand header for the
          history pane. Whole row is the click target; the Clear button and the
          info glyph stop propagation. */}
      <div
        role="button"
        tabIndex={0}
        {...uiLocation('chat.older-sessions')}
        onClick={() => { if (historyOpen) setHistoryOpen(false); else openHistoryPane() }}
        onKeyDown={e => {
          // Only the row's own keys toggle it. A key pressed on a control inside
          // the row (Clear, the info glyph) bubbles here too, and preventing its
          // default would swallow that control's own activation.
          if ((e.target as HTMLElement) !== e.currentTarget) return
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (historyOpen) setHistoryOpen(false); else openHistoryPane() }
        }}
        /* pt/pb are 14px, not py-3, so this row's top border lands on the same
           baseline as the nav rail's community row ("Star us · Report issue"):
           both cards sit 8px off the shell floor, the rail spends 8+2+24+10 =
           44px below its own hairline, and 14+16+14 matches that exactly. The
           symmetric padding is what keeps the clock and label optically centred
           in the band. */
        className="flex justify-between items-center px-3 pt-[14px] pb-[14px] cursor-pointer select-none"
        aria-expanded={historyOpen}
        aria-controls="history-pane"
        aria-label={i18nT('pages.chatSidebar.older_sessions')}
      >
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong leading-none">
          <Clock size={14} className="shrink-0" />
          <span className="leading-none">{i18nT('pages.chatSidebar.older_sessions_2')}</span>
          {/* What the pane lists. The copy restates the server's `exclude_open`
              rule (handlers/sessions_parts/history_reads.py): every saved
              session not open as a tab, any age. Change it when that rule does. */}
          <InfoTip text={i18nT('pages.chatSidebar.older_sessions_tip')} />
        </span>
        {/* Chevron trails the Clear button so the disclosure glyph is the
            rightmost control, and Clear shifts left by the gap rather than
            being pushed off the row's 12px right inset. The gap is 12px, wider
            than the row's other spacing: Clear is destructive (it wipes closed
            sessions behind a single confirm), so a pointer aimed at the collapse
            glyph must not land on it. This trailing position is the pane's ONE
            deliberate exception to the sidebar's leading-chevron grammar
            (#2887): a section header ends with its own disclosure glyph, while
            row-level disclosures (group headers, hidden-folders reveal, the
            folders filter row) lead with theirs like tree rows everywhere else.
            All four share the same mechanic: a ChevronRight that rotates 90°
            when open — never a Right/Down glyph swap, never a counter-rotation
            when closed. */}
        <span className="flex items-center gap-3 shrink-0">
          {historyOpen && history.length > 0 && (
            <button
              className="px-2 py-0.5 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-danger hover:border-danger transition-all"
              onClick={async e => { e.stopPropagation(); await clearClosedSessionsWithConfirm() }}
            >{i18nT('pages.chatSidebar.clear')}</button>
          )}
          <DisclosureChevron open={historyOpen} size={16} className="text-text-strong" />
        </span>
      </div>
      <AnimatePresence initial={false}>
        {historyOpen && (
          <motion.div
            id="history-pane"
            key="history-pane"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <div className="px-2 pb-1">
              <div className="relative">
                <SearchInput className="w-full" placeholder={i18nT('pages.chatSidebar.search_older_sessions')} value={historyFilter} onChange={e => setHistoryFilter(e.target.value)} />
                {historyFilter && (
                  <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors" onClick={() => setHistoryFilter('')} aria-label={i18nT('pages.chatSidebar.clear_search')}><X size={13} /></button>
                )}
              </div>
              {/* The unresumable-surface notice used to live here. It moved to
                  ChatPage's shared notice slot above the composer (#5925): this
                  pane starts CLOSED (`historyOpen` defaults false), so a notice
                  inside it can only ever be seen by someone who had already
                  opened it -- which is nobody arriving from the command palette,
                  a notification, or ChatPage's own "Continue a previous chat"
                  list. One always-visible site serves all of them. */}
            </div>
            {/* scroll-shadow already fades the top/bottom edge as its
             *  scrollability cue, so the bar itself is redundant here. */}
            <div className="overflow-y-auto scrollbar-none p-2 scroll-shadow" style={{ height: `${historyHeight}px`, scrollbarWidth: 'none' }}>
              {(() => {
                const historyLocalMatch = (s: { title?: string; key: string }) =>
                  ((s.title || '') + s.key).toLowerCase().includes(historyFilter.toLowerCase())
                // Additive rather than a boolean OR: here the backend result IS the
                // source list, so filtering `history` instead would drop backend-only hits.
                // Remote crew sessions are NOT merged here: they are the peer's
                // LIVE slots and join the live sessions list above. Merging them into
                // history as well would render each remote row twice.
                const filteredHistory = (() => {
                  if (!historyFilter) return history
                  if (historyFilter.trim().length >= SEARCH_MIN_CHARS && historySearchResults) {
                    const seen = new Set(historySearchResults.map(s => s.key))
                    return [...historySearchResults,
                            ...history.filter(s => !seen.has(s.key) && historyLocalMatch(s))]
                  }
                  return (historySearchResults ?? history).filter(historyLocalMatch)
                })()
                // One definition of "search active" for every site below: results
                // are present AND the query is still at/above the search threshold.
                // The compound check matters on the clear-X frame: historyFilter
                // empties synchronously but useDebouncedSessionSearch nulls its
                // result in a passive effect (one render later), so a bare
                // `historySearchResults` test would treat that stale frame as an
                // active search and paint date segment headers over a
                // relevance-ordered list.
                const searchActive = historyFilter.trim().length >= SEARCH_MIN_CHARS && !!historySearchResults
                // Hide date segments when the user has an active search — results are
                // Segments only make sense when the list is date-ordered. For name/created
                // sorts (or active search, which is relevance-ranked) they'd interleave.
                const showSegments = !searchActive
                  && (sortKey === 'date-desc' || sortKey === 'date-asc')
                // Active search: keep the backend's relevance ranking (title-boosted;
                // see search_sessions in history.py). Re-sorting search results by the
                // sidebar sort key buried an exact title match under fresher sessions
                // that merely mention the query in their content — and defeated
                // groupHistoryByFolder's documented order-preserving contract. The
                // command palette's Sessions tab already preserves backend order.
                // No search: skip the sort only when the backend already returns
                // date-desc order.
                const sortedHistory = (searchActive || sortKey === 'date-desc') ? filteredHistory : [...filteredHistory].sort((a, b) => compareBySort(a, b, sortKey))
                // An empty pane is reachable whenever every session on disk is
                // already open as a tab (the common case for a light user), so it
                // needs to say so rather than render a search box over blank space.
                // A filtered-to-nothing list is a different statement and reuses the
                // wording the two sibling panes already use for it.
                if (sortedHistory.length === 0) {
                  return (
                    <div className="px-3 py-4 text-[12px] text-muted text-center">
                      {historyFilter
                        ? i18nT('pages.chatSidebar.no_sessions_match')
                        : i18nT('pages.chatSidebar.no_older_sessions')}
                    </div>
                  )
                }
                let prevSeg = ''
                // Derive agent color the same way renderSessionRow does so history rows
                // match the session-row visual language (agent name tinted by source).
                const agentColorFor = (agentName: string): string => {
                  const meta = tintAgents.find(a => a.name === agentName)
                  if (meta?.source === 'package') return 'text-[var(--aim)]'
                  if (meta?.source === 'builtin') return 'text-muted'
                  return 'text-muted'
                }
                const historyRow = (s: (typeof sortedHistory)[number]) => {
                  const displayDate = fmtRelativeTime(s.modified ?? s.created)
                  const agentName = s.agent || defaultAgent || ''
                  // Display vs resolution key, same split as renderSessionRow:
                  // `agentColorFor` must receive the bare name. An archived
                  // session whose JSONL metadata never recorded an agent falls
                  // back to the CURRENT default, which is a different fact from
                  // a session pinned to that same alias — the marker is what
                  // tells them apart (#6529).
                  const agentDisplay = agentName ? agentOrDefaultLabel(s.agent, defaultAgent) : ''
                  const agentColor = agentColorFor(agentName)
                  const isDashboard = s.key.startsWith('dashboard')
                  const channel = slotChannelNamespace(s.key)
                  const surfaceLabel = isDashboard
                    ? i18nT('pages.chatSidebar.dashboard_source')
                    : slotChannelLabel(s.key) || i18nT('pages.chatSidebar.session_source')
                  // Federated-search row from a connected remote instance: its
                  // transcript lives on the other gateway, so activation switches
                  // to that instance's pane instead of resuming a (same-keyed but
                  // unrelated) local session, and the local delete action is
                  // hidden — deleteHistorySession would target the LOCAL file.
                  const remoteInstanceId = (s as { instance_id?: string }).instance_id
                  const remoteInstanceName = (s as { instance_name?: string }).instance_name
                  const activateRow = () => {
                    // A remote row never resumes here, so it can never produce the
                    // unresumable notice above — the pane switch IS its outcome.
                    if (remoteInstanceId) { selectInstance(remoteInstanceId); return }
                    // No post-resolve check here: `resumeFromHistory` itself
                    // records an undisplayable-surface answer on the slice
                    // (#5925), which is what the notice above renders. Keeping
                    // a second copy of that predicate per call site is how the
                    // four sibling entry points ended up giving no feedback at
                    // all while this one did.
                    dispatch(resumeFromHistory({ key: s.key, title: s.title || s.key }))
                  }
                  return (
                    <div className={`group relative flex items-start gap-2.5 pr-4 py-2 rounded-md text-sm transition-all select-none ${remoteInstanceId ? '' : '[@media(hover:none)]:pr-10 '}${!connected ? 'text-muted opacity-50 cursor-not-allowed' : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'}`} style={{ paddingLeft: '10px' }} title={sessionDisplayTitle(s)} {...offlineProps(connected, i18nT('utils.offline.resume_sessions'))} role="button" tabIndex={0} aria-disabled={!connected} onKeyDown={e => {
                      // WCAG 2.1.1: history rows must be resumable via keyboard.
                      if (e.key !== 'Enter' && e.key !== ' ') return
                      if ((e.target as HTMLElement) !== e.currentTarget) return
                      e.preventDefault()
                      if (!connected) return
                      activateRow()
                    }} onMouseDown={e => {
                      // NOTE: pointer activation lives on onMouseDown (not onClick). For a
                      // div[role="button"], browsers do NOT synthesize a click from Enter
                      // (that only happens for native buttons/links — hence the onKeyDown
                      // handler above), and AT activation (e.g. VoiceOver VO+Space)
                      // synthesizes a click INSTEAD of key events. So each path activates
                      // exactly once. Do NOT add an e.detail === 0 guard here or in any
                      // future onClick: AT-synthesized clicks have detail 0 and would be
                      // silently dropped, breaking screen-reader activation.
                      e.preventDefault()
                      if ((e.target as HTMLElement).closest?.('[data-close]')) { if (!remoteInstanceId) deleteHistorySessionWithConfirm(s.key); return }
                      if (!connected) return
                      activateRow()
                    }}>
                      {/* Platform glyph — fills the left column that session rows reserve for the unread dot */}
                      <span role="img" className="shrink-0 flex items-center justify-center self-center text-muted" title={surfaceLabel} aria-label={surfaceLabel}>
                        {isDashboard
                          ? <Monitor size={12} />
                          : channel === 'unified'
                            ? <MessageSquare size={12} />
                            : <ChannelBrandIcon channel={channel ?? ''} size={12} />
                        }
                      </span>
                      <div className="flex-1 min-w-0 overflow-hidden">
                        <div className={`session-agent-label text-[11px] font-semibold truncate leading-tight flex items-center gap-1 ${agentColor}`}>
                          <span className="truncate" title={agentDisplay || undefined}>{agentDisplay || '\u00A0'}</span>
                          {/* Remote-crew marker. Tinted `info` + a server glyph rather
                              than the neutral chip styling every other meta chip uses:
                              this row's transcript lives on ANOTHER MACHINE, which is a
                              different claim from "has this tag" and the one the user
                              must not misread. The glyph is the non-colour half of the
                              cue, so the distinction survives a colour-vision
                              deficiency; it is aria-hidden because the crew name beside
                              it already names the target.

                              The tooltip names the OUTCOME, and it is the opposite of a
                              live peer row's. Activating this row calls
                              `selectInstance` — the pane switch IS its outcome — whereas
                              a live peer row opens the session HERE. The two carry the
                              same pill, so without this the identical marker would mean
                              two different clicks. */}
                          {remoteInstanceName && (
                            <RemoteCrewChip
                              name={remoteInstanceName}
                              label={i18nT('pages.chatSidebar.on_instance', { name: remoteInstanceName })}
                              title={i18nT('pages.chatSidebar.opens_on_crew_switches_there', { name: remoteInstanceName })}
                            />
                          )}
                          {s.memory_mode === 'incognito' && <span className="text-muted" title={i18nT('pages.chatSidebar.incognito_no_memory_writes')}><EyeOff size={10} /></span>}
                          {s.memory_mode === 'temporary' && <span className="text-aim" title={i18nT('pages.chatSidebar.temporary_no_memory_reads_or_writes')}><VenetianMask size={10} /></span>}
                          {displayDate && <span className="ml-auto text-[11px] text-muted font-normal shrink-0">{displayDate}</span>}
                        </div>
                        <div className="text-[13px] leading-snug line-clamp-2 break-words">{sessionDisplayTitle(s)}</div>
                      </div>
                      {/* Floating hover button group — matches session-row pattern.
                          Hidden for remote rows: deleteHistorySession targets the
                          LOCAL session file, which for a remote row is at best a
                          same-keyed unrelated conversation. */}
                      {!remoteInstanceId && <div className="absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-visible:opacity-100 group-has-[:focus-visible]:opacity-100 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm">
                        <button type="button" title={i18nT('pages.chatSidebar.delete_history_session')} aria-label={i18nT('pages.chatSidebar.delete_history_session')} className="text-[12px] text-muted cursor-pointer p-[4px] rounded hover:text-danger-text hover:bg-danger-subtle transition-all bg-transparent border-none" onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); deleteHistorySessionWithConfirm(s.key) }}><X size={12} /></button>
                      </div>}
                    </div>
                  )
                }
                // Folder-grouped view: during an active content search, regroup the
                // relevance-ranked results under collapsible folder headers (+ Unfiled)
                // by the folder each session was filed in, instead of date segments.
                if (searchActive) {
                  return groupHistoryByFolder(sortedHistory, folders, folderSortMode).map(({ key: gid, folder, rows }) => {
                    const collapsed = collapsedHistoryGroups.has(gid)
                    const groupName = folder ? folder.name : i18nT('pages.chatSidebar.unfiled')
                    return (
                      <Fragment key={gid}>
                        <button type="button" aria-expanded={!collapsed} aria-label={collapsed ? i18nT('pages.chatSidebar.expand_group_results', { group: groupName }) : i18nT('pages.chatSidebar.collapse_group_results', { group: groupName })} className="w-full flex items-center gap-1.5 px-2 pt-3 pb-1 text-[11px] font-semibold text-muted select-none bg-transparent border-none cursor-pointer hover:text-text first:pt-1" onClick={() => setCollapsedHistoryGroups(prev => { const next = new Set(prev); if (next.has(gid)) next.delete(gid); else next.add(gid); return next })}>
                          <DisclosureChevron open={!collapsed} size={12} />
                          {folder ? <FolderGlyph color={folder.color} icon={folder.icon} size={12} open={!collapsed} /> : <Folder size={12} className="text-muted shrink-0" />}
                          <span className="truncate">{folder ? folder.name : i18nT('pages.chatSidebar.unfiled')}</span>
                          <span className="ml-0.5 text-muted font-normal tabular-nums">· {rows.length}</span>
                        </button>
                        {!collapsed && rows.map((s, i) => (
                          <Fragment key={historyRowIdentity(s)}>
                            {historyRow(s)}
                            {i < rows.length - 1 && <div className="mx-3 border-b border-border" />}
                          </Fragment>
                        ))}
                      </Fragment>
                    )
                  })
                }
                return sortedHistory.map((s, idx) => {
                  const tsForSegment = s.modified ?? s.created
                  const seg = dateSegment(tsForSegment)
                  const showHeader = showSegments && seg !== prevSeg
                  prevSeg = seg
                  // Divider between consecutive rows — but not before a segment header
                  // (the header itself separates), and not after the last row.
                  const isLast = idx === sortedHistory.length - 1
                  const nextSeg = !isLast ? dateSegment(sortedHistory[idx + 1].modified ?? sortedHistory[idx + 1].created) : seg
                  const showDivider = !isLast && (!showSegments || nextSeg === seg)
                  return (
                    <Fragment key={historyRowIdentity(s)}>
                      {showHeader && (
                        <div className="px-2 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {historyRow(s)}
                      {showDivider && <div className="mx-3 border-b border-border" />}
                    </Fragment>
                  )
                })
              })()}
              {/* Load-more uses onMouseDown+preventDefault to trigger without stealing
                  focus from the transcript; scope-disable the static-interaction rule. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
              {historyHasMore && <div className="flex justify-center py-2 text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle rounded-md" onMouseDown={e => { e.preventDefault(); dispatch(fetchHistory(true)) }}>{i18nT('pages.chatSidebar.load_more')}</div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}
