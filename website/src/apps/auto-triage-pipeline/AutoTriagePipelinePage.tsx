// AutoTriagePipelinePage — the page-entry component the builtin registry lazy-
// loads for the `/auto-triage-pipeline` route.
//
// Two views, because they answer different questions from DIFFERENT data:
//   * PIPELINE (default) reads this machine's own pipeline trail: which step every
//     item is in, what each step is moving, and what each session cost.
//   * ITEM LANES reads the crew ledger through Issue Radar's crew-fabric seam and
//     draws one lane per work item across the phase enum.
// They are not two renderings of one dataset, so a tab is honest where a merged
// view would imply the numbers are comparable.
//
// The tab row is the repo's `UnderlineTabs`, not a hand-rolled one. An earlier
// version put `role="tablist"` / `role="tab"` / `aria-selected` on plain buttons
// with no roving tabindex, no arrow-key handling and no `aria-controls` -- which
// ANNOUNCES the tabs keyboard contract to assistive tech and then does not honour
// it. A screen reader said "tab 1 of 2" while the arrow keys did nothing, and that
// is worse than not claiming the roles at all. The shared component exists for
// exactly this.
import { useState } from 'react'
import UnderlineTabs, { type UnderlineTab } from '../../components/UnderlineTabs'
import { i18nT } from '../../i18n/t'
import GlobalPipelineView from './views/GlobalPipelineView'
import PipelineView from './views/PipelineView'

type Tab = 'pipeline' | 'lanes'

export default function AutoTriagePipelinePage() {
  const [tab, setTab] = useState<Tab>('pipeline')

  const tabs: Array<UnderlineTab<Tab>> = [
    { key: 'pipeline', label: i18nT('apps.autoTriagePipeline.global.tab_pipeline') },
    { key: 'lanes', label: i18nT('apps.autoTriagePipeline.global.tab_lanes') },
  ]

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-bg">
      <div className="shrink-0 px-4 pt-3 md:px-6">
        <UnderlineTabs
          tabs={tabs}
          value={tab}
          onChange={setTab}
          ariaLabel={i18nT('apps.autoTriagePipeline.global.tablist_label')}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-hidden">
        {tab === 'pipeline' ? <GlobalPipelineView /> : <PipelineView />}
      </div>
    </div>
  )
}
