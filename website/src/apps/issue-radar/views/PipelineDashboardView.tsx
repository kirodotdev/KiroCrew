// The pipeline dashboard's host: it takes the repository Issue Radar already has
// active and hands it to the two pipeline views.
//
// This component exists for one job the views cannot do for themselves. Both of
// them hold drill-down state — the fold view an open step and an open item, the
// lanes view an expanded lane — and neither keys that state on the repository,
// because as a standalone app each resolved its own repository once and never saw
// it change. Now it can change under them: switching repositories in Issue Radar
// re-renders these views with a different `repo` prop while their open item stays
// open, and the L2 lookup behind it is keyed on the issue NUMBER, so an item
// expanded under one repository would show the other's sessions and credit costs
// under the same number.
//
// The fix is a remount rather than a prop-change: a React `key` that carries the
// repository identity means a switch DESTROYS each view's state instead of
// carrying it across.
//
// The two boards stay TABBED, carried over from the page this replaces along with
// its reason: they answer different questions from different data -- the fold reads
// this machine's pipeline trail (which step each item is in, what each session
// cost), the lanes view reads the crew ledger through Issue Radar's crew-fabric
// seam. Their numbers are not comparable, so one page would imply they are, and only
// the selected board mounts, so a reader looking at one is not paying for the
// other's polling.
//
// The tab row is the repo's shared `ui/tabs.tsx` (Radix Tabs), not a hand-rolled one,
// and that matters for a concrete reason: `role="tablist"` / `role="tab"` /
// `aria-selected` on plain buttons announces a keyboard contract -- roving tabindex,
// arrow keys, `aria-controls` -- that a hand-rolled row does not honour, so a screen
// reader says "tab 1 of 2" while the arrow keys do nothing. Claiming the contract and
// not honouring it is worse than not claiming it.
import { useMemo, useState } from 'react'
import { Tabs, TabsContent, TabsList, TabsTrigger, type TabItem } from '../../../components/ui/tabs'
import { TABS_RAIL_ROW_CLASS } from '../../../components/ui/tabsPill'
import { useIssueRadar } from '../context'
import { i18nT } from '../../../i18n/t'
import { repoScopeKey } from '../lib/links'
import type { RepoRef } from '../api'
import GlobalPipelineView from '../pipeline/views/GlobalPipelineView'
import PipelineView from '../pipeline/views/PipelineView'

type Tab = 'pipeline' | 'lanes'

export default function PipelineDashboardView() {
  const { active, repos } = useIssueRadar()
  const [tab, setTab] = useState<Tab>('pipeline')

  const tabs: Array<TabItem<Tab>> = [
    { key: 'pipeline', label: i18nT('apps.autoTriagePipeline.global.tab_pipeline') },
    { key: 'lanes', label: i18nT('apps.autoTriagePipeline.global.tab_lanes') },
  ]

  // The CONNECTED record, which is the only place the forge half is guaranteed to
  // be. `active` can be slug-only: the host falls back to `{owner, repo}` built
  // from `repos[0]` when nothing is stored or the stored repository is no longer
  // connected, and its membership test compares owner and repo alone, so a stored
  // slug-only pointer is returned unenriched. Sending that identity would omit
  // `provider`, which the backend reads as public GitHub -- so a GitLab repository
  // would quietly be served GitHub's trail, issue cache and queue shard under its
  // own heading. That substitution is exactly what the backend's refusal exists to
  // prevent, and it cannot fire on a request that never says which forge it means.
  const connected = repos.find((r) => r.owner === active.owner && r.repo === active.repo)

  // Memoised on the FIELDS, not the object: the context hands out a fresh
  // `active` object on unrelated updates (a poll tick, an issue list refresh),
  // and a fresh `repo` prop would re-render both children and churn their query
  // keys for no reason. This is a re-render optimisation only -- it is NOT what
  // protects the drill-down. That is `scopeKey` below, which is a STRING: an
  // unrelated context update produces the same string, so the key is unchanged
  // and nothing remounts even if this object were rebuilt every render.
  const provider = active.provider ?? connected?.provider
  const host = active.host ?? connected?.host
  const repo = useMemo<RepoRef>(
    () => ({
      owner: active.owner,
      repo: active.repo,
      ...(provider ? { provider } : {}),
      ...(host ? { host } : {}),
    }),
    [active.owner, active.repo, provider, host],
  )

  // Issue Radar's own repository-identity key, the same one its caches use. Shared
  // rather than re-derived: a second spelling of "which repository is this" is how
  // one repository ends up keyed two ways inside one app, and this component exists
  // precisely to stop a switch from being missed.
  //
  // For every repository that gets PAST the refusal below the forge half is
  // constant, so today the slug does the distinguishing. It is still the right key:
  // it is what stays correct if this board ever accepts a second forge.
  const scopeKey = repoScopeKey(repo)

  return (
    <Tabs
      value={tab}
      onValueChange={v => setTab(v as Tab)}
      layoutId="issue-radar-pipeline"
      className="flex h-full min-h-0 flex-col overflow-hidden"
    >
      <div className={`shrink-0 ${TABS_RAIL_ROW_CLASS}`}>
        <TabsList aria-label={i18nT('apps.autoTriagePipeline.global.tablist_label')}>
          {tabs.map(t => (
            <TabsTrigger key={t.key} value={t.key}>{t.label}</TabsTrigger>
          ))}
        </TabsList>
      </div>

      {/* Only the selected board mounts, so a reader looking at one is not paying
          for the other's polling — Radix unmounts the inactive panel. */}
      <TabsContent value="pipeline" className="min-h-0 flex-1 overflow-hidden">
        <GlobalPipelineView key={`fold:${scopeKey}`} repo={repo} />
      </TabsContent>
      <TabsContent value="lanes" className="min-h-0 flex-1 overflow-hidden">
        <PipelineView key={`lanes:${scopeKey}`} repo={repo} />
      </TabsContent>
    </Tabs>
  )
}
