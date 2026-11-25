/**
 * The memory page's frame for one store: one header row (the store switcher at
 * left; Copy memories, New memory and ⋯ at right), the embedded count under it,
 * the underline tabs and their panels. The global page and a bot's page each fill
 * the panels with their own cards.
 */
import { useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Copy, Lightbulb, ListChecks, MoreHorizontal, Plus, Settings, Sparkles, X } from 'lucide-react'
import { Btn, IconButton } from '../../../components/ui'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../../components/ui/tabs'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { MEMORY_QUERY_PREFIXES } from '../MemoryStoreCard'
import { NewLessonDialog, NewMemoryDialog } from './MemoryNewDialogs'
import { MemoryActionSheet, type SheetAction } from './MemoryActionSheet'

export type MemoryPageTab = 'memories' | 'profile' | 'recovery' | 'failures' | 'settings'
export type MemoryListControls = { selecting: boolean; setSelecting: (on: boolean) => void; recallOpen: boolean; setRecallOpen: (open: boolean) => void; openTab: (tab: MemoryPageTab) => void }
const TABS: [MemoryPageTab, string][] = [
  ['memories', 'memoryV2.tab_memories'], ['profile', 'memoryV2.tab_profile'], ['recovery', 'memoryV2.tab_recovery'],
  ['failures', 'memoryPage.tab_failures'], ['settings', 'memoryPage.tab_settings'],
]

export default function MemoryPageShell({ id, label, switcher, notices, embedded, initialTab = 'memories', mountAll = false, canCreate, canRecall, onCopy, copyDisabled, onCreated, onDraftChange, memories, panels }: {
  /** Distinguishes this store's tab underline from another store's as it slides. */
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
   *  state the other panels read); otherwise a panel mounts when first opened. */
  mountAll?: boolean
  /** New memory and New lesson write the global store only. */
  canCreate: boolean
  /** Test recall reads a bot's own memory. */
  canRecall: boolean
  onCopy?: () => void
  copyDisabled?: boolean
  onCreated?: () => void
  /** A New memory or New lesson draft is open and unsaved: feeds the page's leave guard. Pass a stable callback. */
  onDraftChange?: (dirty: boolean) => void
  memories: (controls: MemoryListControls) => ReactNode
  panels: Record<Exclude<MemoryPageTab, 'memories'>, ReactNode>
}) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const phone = useIsMobile()
  const [tab, setTab] = useState<MemoryPageTab>(initialTab)
  const [visited, setVisited] = useState<Set<MemoryPageTab>>(() => new Set([initialTab]))
  const [selecting, setSelecting] = useState(false)
  const [recallOpen, setRecallOpen] = useState(false)
  const [creating, setCreating] = useState<'memory' | 'lesson' | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [saved, setSaved] = useState('')
  const open = (next: MemoryPageTab) => { setTab(next); setVisited(old => new Set([...old, next])) }
  const created = (message: string) => {
    setCreating(null); setSaved(message)
    void client.invalidateQueries({ queryKey: ['memory-records'] })
    for (const prefix of MEMORY_QUERY_PREFIXES) void client.invalidateQueries({ queryKey: prefix })
    onCreated?.()
  }
  // On a phone the list's own Select and Test recall, and Copy memories, move into the ⋯ sheet.
  const listActions: SheetAction[] = phone ? [
    { key: 'select', icon: ListChecks, label: t('memoryPage.select'), run: () => { open('memories'); setSelecting(true) } },
    ...(onCopy ? [{ key: 'copy', icon: Copy, label: t('memoryV2.copy_action'), run: onCopy, disabled: copyDisabled }] : []),
    ...(canRecall ? [{ key: 'recall', icon: Sparkles, label: t('memoryPage.test_recall'), run: () => { open('memories'); setRecallOpen(true) } }] : []),
  ] : []
  const createActions: SheetAction[] = canCreate ? [{ key: 'lesson', icon: Lightbulb, label: t('memoryPage.new_lesson'), run: () => setCreating('lesson') }] : []
  const more = listActions.length + createActions.length > 0
  const moreButtonClass = 'flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border border-border sm:h-9 sm:w-9'
  const panel = (key: MemoryPageTab, content: ReactNode) => <TabsContent key={key} value={key} forceMount hidden={tab !== key} className="space-y-4">{(mountAll || visited.has(key)) && content}</TabsContent>
  return <div className="min-w-0 space-y-4">
    <header className="space-y-3">
      <div className="flex min-w-0 items-center justify-between gap-1.5 sm:gap-2">
        <div className="min-w-0">{switcher}</div>
        <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
          {!phone && onCopy && <Btn className="min-h-9" disabled={copyDisabled} onClick={onCopy}><Copy className="lucide-inline" />{t('memoryV2.copy_action')}</Btn>}
          {canCreate && <Btn primary className="min-h-11 sm:min-h-9" onClick={() => setCreating('memory')}><Plus className="lucide-inline" />{t('memoryPage.new_memory')}</Btn>}
          {more && (phone
            ? <IconButton aria-label={t('memoryPage.more_actions')} aria-haspopup="dialog" className={moreButtonClass} onClick={() => setSheetOpen(true)}><MoreHorizontal className="lucide-inline" aria-hidden="true" /></IconButton>
            : <DropdownMenu>
              <DropdownMenuTrigger asChild><IconButton aria-label={t('memoryPage.more_actions')} className={moreButtonClass}><MoreHorizontal className="lucide-inline" aria-hidden="true" /></IconButton></DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-52">
                {createActions.map(item => <DropdownMenuItem key={item.key} onSelect={item.run}><item.icon className="lucide-inline text-muted" aria-hidden="true" />{item.label}</DropdownMenuItem>)}
              </DropdownMenuContent>
            </DropdownMenu>)}
        </div>
      </div>
      {embedded}
      {notices}
    </header>
    {saved && <div role="status" className="flex items-center gap-2 rounded-lg bg-ok-subtle px-3 py-2 text-[13px] text-ok-text"><span className="min-w-0 flex-1 break-words">{saved}</span><IconButton aria-label={t('components.errorNotice.dismiss')} className="flex h-8 w-8 items-center justify-center" onClick={() => setSaved('')}><X className="lucide-inline" aria-hidden="true" /></IconButton></div>}
    <Tabs variant="underline" layoutId={`memory-page-tabs-${id}`} value={tab} onValueChange={value => open(value as MemoryPageTab)}>
      <TabsList aria-label={t('memoryV2.title', { member: label })} className="mb-4">
        {TABS.map(([key, text]) => {
          // A phone shows four text tabs and a gear for Settings at the end of the rule.
          const gear = phone && key === 'settings'
          return <TabsTrigger key={key} value={key} aria-label={gear ? t(text) : undefined} className={gear ? 'ml-auto w-11 px-0' : phone ? 'px-2.5' : undefined}>
            {gear ? <Settings className="lucide-inline" aria-hidden="true" /> : t(text)}
          </TabsTrigger>
        })}
      </TabsList>
      {panel('memories', memories({ selecting, setSelecting, recallOpen, setRecallOpen, openTab: open }))}
      {panel('profile', panels.profile)}
      {panel('recovery', panels.recovery)}
      {panel('failures', panels.failures)}
      {panel('settings', panels.settings)}
    </Tabs>
    {sheetOpen && <MemoryActionSheet title={label} groups={[listActions, createActions]} onClose={() => setSheetOpen(false)} />}
    {creating === 'memory' && <NewMemoryDialog onDirtyChange={onDraftChange} onClose={() => setCreating(null)} onSaved={key => created(t('memoryPage.saved_key', { key }))} />}
    {creating === 'lesson' && <NewLessonDialog onDirtyChange={onDraftChange} onClose={() => setCreating(null)} onSaved={() => created(t('pages.overview.memoryFailuresCard.lesson_saved'))} />}
  </div>
}
