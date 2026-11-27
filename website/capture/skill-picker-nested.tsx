/**
 * Isolated capture entry for the composer's `$` skill picker over nested skills
 * (#42694): group folders carry a short leaf, the SKILL.md `name:` carries the
 * full name, so the row shows the name as its main label and the path on the
 * second line next to the description.
 *
 * The component is the real one, imported unmodified; only `api.skills` is a
 * fixture, stubbed on the api client the same way the suite stubs it. What a
 * screenshot falsifies here and a test cannot: that the path reads as a smaller
 * second line rather than competing with the name, that a long path plus a
 * description still fit one row without wrapping, and that both lines carry a
 * theme token so neither disappears in dark mode.
 *
 * WHY ISOLATED: the picker is a portalled menu anchored to a measured rect inside
 * the chat composer, which needs the app shell, a live websocket and a seeded
 * session to reach; a half-stubbed shell renders its error boundary instead.
 *
 * `theme` and `q` come from the query string: ?theme=light&q=ops. An empty `q`
 * lists the whole catalog.
 */
import { createRoot } from 'react-dom/client'
import { useRef } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SkillPickerMenu from '../src/components/SkillPickerMenu'
import { api } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)
const query = params.get('q') ?? ''

// The five skills from the issue, plus one flat skill whose name, folder and key
// coincide, so the frame shows the unchanged single-line case beside the new one.
const SKILLS = [
  { key: 'finance/reports/quarterly-close', name: 'finance-reports-quarterly-close', description: 'Close the quarter: reconcile, accrue, report', source: 'kirocrew' },
  { key: 'ops/oncall/handoff-summary', name: 'ops-oncall-handoff-summary', description: 'Summarize the on-call week for the next engineer', source: 'kirocrew' },
  { key: 'data/warehouse/query-lineage', name: 'data-warehouse-query-lineage', description: 'Trace a query back to its source tables', source: 'kirocrew' },
  { key: 'data/warehouse/freshness-check', name: 'data-warehouse-freshness-check', description: 'Check table freshness against SLAs', source: 'kirocrew' },
  { key: 'support/triage/escalation', name: 'support-triage-escalation', description: 'Escalate a case with the right context', source: 'kirocrew' },
  // A second group ending in the same leaf: both rows list, told apart by the path.
  { key: 'ops/oncall/escalation', name: 'ops-oncall-escalation', description: 'Page the on-call and hand over context', source: 'kirocrew' },
  { key: 'grill', name: 'grill', description: 'Structured questioning before action', source: 'kirocrew' },
]
api.skills = async () => SKILLS

/** The anchor the chat composer would be: bottom of the viewport, so the menu
 *  opens upward exactly as it does in the dashboard. */
function Scene() {
  const anchor = useRef<HTMLDivElement>(null)
  return (
    <div style={{ height: '100vh', position: 'relative' }}>
      <div
        ref={anchor}
        style={{ position: 'absolute', bottom: 16, left: 16, right: 16 }}
        className="px-3 py-2 text-[13px] font-mono text-text border border-border rounded-lg bg-card"
      >
        ${query}
      </div>
      <SkillPickerMenu query={query} anchorRef={anchor} open onSelect={() => {}} onClose={() => {}} />
    </div>
  )
}

initI18n('en')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Scene />
  </QueryClientProvider>,
)
