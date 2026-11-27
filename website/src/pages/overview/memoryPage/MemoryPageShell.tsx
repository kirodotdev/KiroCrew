/**
 * The memory page's frame for one store: one header row (the store switcher at
 * left; Copy memories, New memory and ⋯ at right), the embedded count under it,
 * the underline tabs and their panels. The global page and a bot's page each fill
 * the panels with their own cards.
 */
import { useState, type ReactNode } from 'react'
import { MemoryJumpContext, type MemoryJump } from './memoryJump'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Copy, ListChecks, MoreHorizontal, Plus, Settings, Sparkles, X } from 'lucide-react'
import { Btn, IconButton } from '../../../components/ui'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../../components/ui/tabs'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { MEMORY_QUERY_PREFIXES } from '../MemoryStoreCard'
import { NewMemoryDialog } from './MemoryNewDialogs'
import { MemoryActionSheet, type SheetAction } from './MemoryActionSheet'
import { MemoryRecallDialog } from './MemoryRecallDialog'
import { uiLocation } from '../../../uiLocations/uiLocation'

export type MemoryPageTab = 'memories' | 'graph' | 'profile' | 'recovery' | 'failures' | 'settings'
export type MemoryListControls = { selecting: boolean; setSelecting: (on: boolean) => void; openTab: (tab: MemoryPageTab) => void
  /** The Graph tab's last hand-off to the list. */
  jump: MemoryJump | null }
const TABS: [MemoryPageTab, string][] = [
  ['memories', 'memoryV2.tab_memories'], ['graph', 'memoryPage.tab_graph'], ['profile', 'memoryV2.tab_profile'], ['recovery', 'memoryV2.tab_recovery'],
  ['failures', 'memoryPage.tab_failures'], ['settings', 'memoryPage.tab_settings'],
]

export default function MemoryPageShell({ id, label, switcher, notices, embedded, initialTab = 'memories', mountAll = false, canCreate, canRecall, onCopy, copyDisabled, onCreated, onDraftChange, memories, panels }: {
  /** The store's id: Test recall reads it, and it keeps this store's tab underline apart from another store's as it slides. */
  id: string
  /** The store's name: the tab list's accessible name and the phone sheet's title. */
  label: string
  /** The store switcher, at the left of the header row. */
  switcher: ReactNode
  /** Store notices (a refused store list, a missing database), under the header row. */
  notices?: ReactNode
  /** The embedded count line, shown while vector memory is on. */
  embedded?: ReactNode
  initialTab?: MemoryPageTab
  /** Mount every panel at once (the global page, whose Settings cards report
   *  state the other panels read); otherwise a panel mounts when first opened.
   *  The graph always waits for its first visit: it reads the whole store. */
  mountAll?: boolean
  /** New memory and New lesson write the global store only. */
  canCreate: boolean
  /** Test recall: what a bot gets from this store. */
  canRecall: boolean
  onCopy?: () => void
  copyDisabled?: boolean
  onCreated?: () => void
  /** A New memory or New lesson draft is open and unsaved: feeds the page's leave guard. Pass a stable callback. */
  onDraftChange?: (dirty: boolean) => void
  memories: (controls: MemoryListControls) => ReactNode
  /** Every tab's panel but Memories. */
  panels: Record<Exclude<MemoryPageTab, 'memories'>, ReactNode>
}) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const phone = useIsMobile()
  const [tab, setTab] = useState<MemoryPageTab>(initialTab)
  const [visited, setVisited] = useState<Set<MemoryPageTab>>(() => new Set([initialTab]))
  const [selecting, setSelecting] = useState(false)
  const [recallOpen, setRecallOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [saved, setSaved] = useState('')
  const open = (next: MemoryPageTab) => { setTab(next); setVisited(old => new Set([...old, next])) }
  // A deep link (?focus=failures) that lands while the page is open changes
  // initialTab: follow it, and only then, so a rerender keeps a tab the user clicked.
  const [linkedTab, setLinkedTab] = useState(initialTab)
  if (linkedTab !== initialTab) { setLinkedTab(initialTab); open(initialTab) }
  const [jump, setJump] = useState<MemoryJump | null>(null)
  const jumpTo = (refs: string[], record?: MemoryJump['open']) => { setJump(old => ({ refs, open: record, seq: (old?.seq || 0) + 1 })); open('memories') }
  const created = (message: string) => {
    setCreating(false); setSaved(message)
    void client.invalidateQueries({ queryKey: ['memory-records'] })
    for (const prefix of MEMORY_QUERY_PREFIXES) void client.invalidateQueries({ queryKey: prefix })
    onCreated?.()
  }
  // Select moves into the phone sheet; Test recall lives in ⋯ at every width.
  const listActions: SheetAction[] = [
    ...(phone ? [{ key: 'select', icon: ListChecks, label: t('memoryPage.select'), run: () => { open('memories'); setSelecting(true) } }] : []),
    ...(canRecall ? [{ key: 'recall', icon: Sparkles, label: t('memoryPage.test_recall'), run: () => setRecallOpen(true) }] : []),
  ]
  // Rare actions live in ⋯ on every width.
  const createActions: SheetAction[] = [
    ...(onCopy ? [{ key: 'copy', icon: Copy, label: t('memoryPage.copy_from'), run: onCopy, disabled: copyDisabled }] : []),
  ]
  const more = listActions.length + createActions.length > 0
  const moreButtonClass = 'flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border border-border sm:h-9 sm:w-9'
  const panel = (key: MemoryPageTab, content: ReactNode) => <TabsContent key={key} value={key} forceMount hidden={tab !== key}><div className="space-y-4">{((mountAll && key !== 'graph') || visited.has(key)) && content}</div></TabsContent>
  return <div className="min-w-0 space-y-4">
    <header className="space-y-3">
      <div className="flex min-w-0 items-center justify-between gap-1.5 sm:gap-2">
        <div className="min-w-0">{switcher}</div>
        <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
          {canCreate && <Btn primary className="min-h-11 sm:min-h-9" onClick={() => setCreating(true)}><Plus className="lucide-inline" />{t('memoryPage.new_memory')}</Btn>}
          {more && (phone
            ? <IconButton aria-label={t('memoryPage.more_actions')} aria-haspopup="dialog" className={moreButtonClass} onClick={() => setSheetOpen(true)}><MoreHorizontal className="lucide-inline" aria-hidden="true" /></IconButton>
            : <DropdownMenu>
              <DropdownMenuTrigger asChild><IconButton {...uiLocation('memory.more-actions')} aria-label={t('memoryPage.more_actions')} className={moreButtonClass}><MoreHorizontal className="lucide-inline" aria-hidden="true" /></IconButton></DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-52">
                {canRecall && <DropdownMenuItem {...uiLocation('memory.test-recall')} onSelect={listActions[0].run}><Sparkles className="lucide-inline text-muted" aria-hidden="true" />{t('memoryPage.test_recall')}</DropdownMenuItem>}
                {createActions.map(item => <DropdownMenuItem key={item.key} disabled={item.disabled} onSelect={item.run}><item.icon className="lucide-inline text-muted" aria-hidden="true" />{item.label}</DropdownMenuItem>)}
              </DropdownMenuContent>
            </DropdownMenu>)}
        </div>
      </div>
      {embedded}
      {notices}
    </header>
    {saved && <div role="status" className="flex items-center gap-2 rounded-lg bg-ok-subtle px-3 py-2 text-[13px] text-ok-text"><span className="min-w-0 flex-1 break-words">{saved}</span><IconButton aria-label={t('components.errorNotice.dismiss')} className="flex h-8 w-8 items-center justify-center" onClick={() => setSaved('')}><X className="lucide-inline" aria-hidden="true" /></IconButton></div>}
    <MemoryJumpContext.Provider value={jumpTo}>
    <Tabs variant="underline" layoutId={`memory-page-tabs-${id}`} value={tab} onValueChange={value => open(value as MemoryPageTab)}>
      {/* A phone scrolls the tab row sideways when the tabs do not fit. */}
      <TabsList aria-label={t('memoryV2.title', { member: label })} className={`mb-4 ${phone ? 'overflow-x-auto overflow-y-hidden scrollbar-none' : ''}`}>
        {TABS.map(([key, text]) => {
          // A phone shows the text tabs and a gear for Settings at the end of the rule.
          const gear = phone && key === 'settings'
          return <TabsTrigger key={key} value={key} aria-label={gear ? t(text) : undefined} className={gear ? 'ml-auto w-11 shrink-0' : phone ? 'shrink-0' : undefined}>
            {gear ? <Settings className="lucide-inline" aria-hidden="true" /> : t(text)}
          </TabsTrigger>
        })}
      </TabsList>
      {panel('memories', memories({ selecting, setSelecting, openTab: open, jump }))}
      {panel('graph', panels.graph)}
      {panel('profile', panels.profile)}
      {panel('recovery', panels.recovery)}
      {panel('failures', panels.failures)}
      {panel('settings', panels.settings)}
    </Tabs>
    </MemoryJumpContext.Provider>
    {recallOpen && <MemoryRecallDialog store={id} onClose={() => setRecallOpen(false)} />}
    {sheetOpen && <MemoryActionSheet title={label} groups={[listActions, createActions]} onClose={() => setSheetOpen(false)} />}
    {creating && <NewMemoryDialog onDirtyChange={onDraftChange} onClose={() => setCreating(false)} onSaved={key => created(t('memoryPage.saved_key', { key }))} onLessonSaved={() => created(t('pages.overview.memoryFailuresCard.lesson_saved'))} />}
  </div>
}
