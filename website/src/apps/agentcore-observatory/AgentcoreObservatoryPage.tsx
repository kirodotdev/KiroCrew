/**
 * AgentCore Observatory — grouped resource rail plus content pane.
 *
 * Three structural decisions this file exists to hold:
 *
 * 1. **Lazy per-type loading.** 27 resource types, each read forking an `aws`
 *    CLI subprocess. Fetching them all for one screen would be tens of seconds
 *    before first paint, so the rail renders from `/catalog` (no AWS call) and a
 *    type is fetched only when it is selected.
 * 2. **The rail carries 17 root types, not 27.** Ten types cannot be listed
 *    standalone — the API requires a parent identifier — so they appear as
 *    sub-lists inside their parent row's detail. That is the API's shape, not a
 *    layout preference.
 * 3. **A count is never the whole answer.** An earlier version rendered each
 *    type as `Evaluators 32`, which tells a reader nothing they can act on. Every
 *    type now renders rows, and a row opens to its raw JSON.
 *
 * The connection control is always present rather than a one-shot setup step: an
 * operator moves between accounts and regions inside one session, and a form that
 * vanishes after the first save strands them.
 *
 * Credentials never appear here. The control takes a profile NAME and a region;
 * the `aws` CLI resolves the credential itself, out of process.
 */
import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Telescope, RefreshCw, AlertTriangle, Ban, Scissors, ChevronRight } from 'lucide-react'
import {
  Btn,
  Card,
  CardTitle,
  ContentSkeleton,
  EmptyState,
  Input,
  PageHeader,
} from '../../components/ui'
import { i18nT } from '../../i18n/t'
import {
  observatoryApi,
  ObservatoryError,
  type ChildType,
  type ListResult,
  type RootType,
} from './api'
import { GROUP_LABEL_KEY, TYPE_LABEL_KEY, rowBadges, rowKey, rowName } from './labels'

const PROFILE_LIST_ID = 'agentcore-observatory-profiles'

/** A type or group with no catalog label falls back to its id, never to blank. */
function labelFor(map: Record<string, string>, id: string): string {
  const key = map[id]
  return key ? i18nT(key) : id
}

/** Wrap a thrown client error in the same shape a read failure uses. */
function asProblem(err: unknown): ListResult {
  return {
    ok: false,
    items: [],
    denied: false,
    truncated: false,
    error: err instanceof ObservatoryError ? err.message : String(err),
  }
}

/** Render a failure with its reason CLASS named, not just "error". */
function Problem({ result }: { result: { denied: boolean; error: string } }) {
  const Icon = result.denied ? Ban : AlertTriangle
  const headline = result.denied
    ? i18nT('apps.agentcoreObservatory.page.denied_headline')
    : i18nT('apps.agentcoreObservatory.page.error_headline')
  return (
    <div className="flex gap-2 items-start text-sm">
      <Icon size={16} className="lucide-inline mt-0.5 text-warn shrink-0" />
      <div className="min-w-0">
        <div className="font-medium">{headline}</div>
        {/* The backend's prose is untranslated, but suppressing it would leave the
            reader with no way to tell an expired session from a missing policy. */}
        <div className="text-muted break-words">{result.error}</div>
      </div>
    </div>
  )
}

function RawJson({ value }: { value: unknown }) {
  return (
    <pre className="text-xs bg-bg rounded border border-border p-3 overflow-x-auto max-h-80">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

/** One child type's rows, fetched with the parent identifiers it declares. */
function ChildList({ child, parentIds }: { child: ChildType; parentIds: Record<string, string> }) {
  const ready = child.parentParams.every((p) => parentIds[p])
  const [openChild, setOpenChild] = useState('')
  const query = useQuery({
    queryKey: ['agentcore-observatory', 'child', child.id, parentIds],
    queryFn: () => observatoryApi.getResource(child.id, parentIds),
    enabled: ready,
  })
  const list = query.data?.list

  return (
    <div className="mt-3">
      <div className="text-sm font-medium mb-1">{labelFor(TYPE_LABEL_KEY, child.id)}</div>
      {!ready ? (
        <p className="text-sm text-muted">
          {i18nT('apps.agentcoreObservatory.page.child_needs_parent')}
        </p>
      ) : query.isLoading ? (
        <ContentSkeleton />
      ) : query.isError ? (
        <Problem result={asProblem(query.error)} />
      ) : !list?.ok ? (
        <Problem result={list ?? { denied: false, error: 'unknown' }} />
      ) : list.items.length === 0 ? (
        <p className="text-sm text-muted">
          {i18nT('apps.agentcoreObservatory.page.none_deployed')}
        </p>
      ) : (
        <ul className="text-sm divide-y divide-border">
          {list.items.map((row, i) => {
            const key = rowKey(row, i)
            const badges = rowBadges(row)
            return (
              <li key={key}>
                <button
                  type="button"
                  onClick={() => setOpenChild(openChild === key ? '' : key)}
                  aria-expanded={openChild === key}
                  className="w-full flex gap-2 items-center py-1.5 text-left hover:bg-bg-hover"
                >
                  <ChevronRight
                    size={14}
                    className={`lucide-inline shrink-0 text-muted transition-transform ${
                      openChild === key ? 'rotate-90' : ''
                    }`}
                  />
                  <span className="truncate">
                    {rowName(row) || i18nT('apps.agentcoreObservatory.page.unnamed_row')}
                  </span>
                  {badges.map((b) => (
                    <span key={b} className="text-muted shrink-0">
                      {b}
                    </span>
                  ))}
                </button>
                {openChild === key && (
                  <div className="pb-2 pl-5">
                    <RawJson value={row} />
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

/** One row, expandable to its raw JSON and any child sub-lists. */
function ResourceRow({
  row,
  type,
  expanded,
  onToggle,
}: {
  row: Record<string, unknown>
  type: RootType
  expanded: boolean
  onToggle: () => void
}) {
  const name = rowName(row) || i18nT('apps.agentcoreObservatory.page.unnamed_row')
  const badges = rowBadges(row)
  // A child needs its parent's OWN id field, which the catalog names — the row is
  // not searched for something that looks like an id.
  const ownId = typeof row[type.idField] === 'string' ? (row[type.idField] as string) : ''

  return (
    <li className="border-b border-border last:border-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="w-full flex gap-2 items-center py-2 text-left hover:bg-bg-hover"
      >
        <ChevronRight
          size={16}
          className={`lucide-inline shrink-0 text-muted transition-transform ${expanded ? 'rotate-90' : ''}`}
        />
        <span className="font-medium truncate">{name}</span>
        {badges.map((b) => (
          <span key={b} className="text-muted text-sm shrink-0">
            {b}
          </span>
        ))}
      </button>
      {expanded && (
        <div className="pb-3 pl-6">
          <RawJson value={row} />
          {type.children.map((child) => (
            <ChildList
              key={child.id}
              child={child}
              parentIds={Object.fromEntries(
                child.parentParams.map((param, i) => [
                  param,
                  // Positional pairing declared by the catalog. Only the row's own
                  // field is available here; a grandchild's extra ancestor field is
                  // supplied when that deeper list is opened.
                  child.parentFields[i] === type.idField ? ownId : '',
                ]),
              )}
            />
          ))}
        </div>
      )}
    </li>
  )
}

/** The selected type's content: a list of rows, or a singleton object. */
function TypePane({ type }: { type: RootType }) {
  const [openRow, setOpenRow] = useState('')
  const query = useQuery({
    queryKey: ['agentcore-observatory', 'resource', type.id],
    queryFn: () => observatoryApi.getResource(type.id),
  })

  useEffect(() => setOpenRow(''), [type.id])

  const list = query.data?.list
  const singleton = query.data?.singleton

  return (
    <Card>
      <CardTitle>
        {labelFor(TYPE_LABEL_KEY, type.id)}
        {/* A count is fine HERE and was not fine as a card: the rows it counts
            are directly below it, so it reads as scale rather than as the whole
            answer. Shown only for a successful read, so it can never dress up a
            failure as "0". */}
        {list?.ok && (
          <span className="ml-2 text-sm font-normal text-muted">{list.items.length}</span>
        )}
      </CardTitle>
      {query.isLoading ? (
        <ContentSkeleton />
      ) : query.isError ? (
        <Problem result={asProblem(query.error)} />
      ) : singleton ? (
        // `token-vault` has no list operation; it is one object.
        !singleton.ok ? (
          <Problem result={singleton} />
        ) : (
          <RawJson value={singleton.item} />
        )
      ) : !list?.ok ? (
        <Problem result={list ?? { denied: false, error: 'unknown' }} />
      ) : list.items.length === 0 ? (
        // Deliberately not an error style: a healthy read of an empty region.
        <p className="text-sm text-muted">
          {i18nT('apps.agentcoreObservatory.page.none_deployed')}
        </p>
      ) : (
        <>
          {list.truncated && (
            <div className="flex gap-2 items-center text-sm text-warn mb-2">
              <Scissors size={16} className="lucide-inline shrink-0" />
              {i18nT('apps.agentcoreObservatory.page.truncated')}
            </div>
          )}
          <ul>
            {list.items.map((row, i) => {
              const key = rowName(row) || String(i)
              return (
                <ResourceRow
                  key={key}
                  row={row}
                  type={type}
                  expanded={openRow === key}
                  onToggle={() => setOpenRow(openRow === key ? '' : key)}
                />
              )
            })}
          </ul>
        </>
      )}
    </Card>
  )
}

export default function AgentcoreObservatoryPage() {
  const queryClient = useQueryClient()
  const catalog = useQuery({
    queryKey: ['agentcore-observatory', 'catalog'],
    queryFn: observatoryApi.getCatalog,
  })
  // Suggestions only, so a failure here must not surface as a page error.
  const profiles = useQuery({
    queryKey: ['agentcore-observatory', 'profiles'],
    queryFn: observatoryApi.getProfiles,
    retry: false,
  })

  const cfg = catalog.data?.config
  const groups = catalog.data?.groups ?? []

  const [selected, setSelected] = useState('')
  const [profile, setProfile] = useState('')
  const [region, setRegion] = useState('')

  // Seed the fields from the saved config so the control shows what is in effect
  // and edits start from it, rather than from blank.
  useEffect(() => {
    if (!cfg) return
    setProfile(cfg.profile)
    setRegion(cfg.region)
  }, [cfg?.profile, cfg?.region, cfg])

  const apply = useMutation({
    mutationFn: () => observatoryApi.saveConfig(profile.trim(), region.trim()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['agentcore-observatory'] })
    },
  })

  /** Picking a known profile fills in the region that profile declares.
   *
   * Not a guess: the registry stores what the profile says about itself, which is
   * authoritative in a way a default region never is. An explicit edit wins. */
  const onProfileChange = (next: string) => {
    setProfile(next)
    const known = profiles.data?.profiles.find((p) => p.name === next)
    if (known?.region) setRegion(known.region)
  }

  const dirty = !!cfg && (profile.trim() !== cfg.profile || region.trim() !== cfg.region)
  const canApply = !!region.trim() && !apply.isPending && (dirty || !cfg?.configured)
  const selectedType = groups.flatMap((g) => g.types).find((t) => t.id === selected)

  return (
    <>
      <PageHeader
        title={i18nT('apps.agentcoreObservatory.page.title')}
        subtitle={i18nT('apps.agentcoreObservatory.page.subtitle')}
        actions={
          <Btn
            onClick={() => void queryClient.invalidateQueries({ queryKey: ['agentcore-observatory'] })}
            disabled={catalog.isFetching}
          >
            <RefreshCw size={16} className="lucide-inline" />
            {i18nT('apps.agentcoreObservatory.page.refresh')}
          </Btn>
        }
      />
      <div className="px-2 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <Card className="mb-6">
          <CardTitle>{i18nT('apps.agentcoreObservatory.page.connect_title')}</CardTitle>
          {/* No region default is offered on purpose: a guessed region returns an
              empty list, which reads as a healthy account with nothing in it. */}
          <p className="text-sm text-muted mb-3">
            {i18nT('apps.agentcoreObservatory.page.connect_help')}
          </p>
          <div className="flex flex-wrap gap-2 items-center">
            <datalist id={PROFILE_LIST_ID}>
              {(profiles.data?.profiles ?? []).map((p) => (
                // The text child, not just `value`, gives the suggestion a name.
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </datalist>
            <Input
              list={PROFILE_LIST_ID}
              value={profile}
              onChange={(e) => onProfileChange(e.target.value)}
              placeholder={i18nT('apps.agentcoreObservatory.page.profile_placeholder')}
              aria-label={i18nT('apps.agentcoreObservatory.page.profile_label')}
            />
            <Input
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              placeholder={i18nT('apps.agentcoreObservatory.page.region_placeholder')}
              aria-label={i18nT('apps.agentcoreObservatory.page.region_label')}
            />
            <Btn onClick={() => apply.mutate()} disabled={!canApply}>
              {i18nT('apps.agentcoreObservatory.page.connect_action')}
            </Btn>
            {cfg?.configured && !dirty && (
              <span className="text-sm text-muted">
                {i18nT('apps.agentcoreObservatory.page.in_effect')}
              </span>
            )}
          </div>
          {apply.isError && (
            <div className="mt-3">
              <Problem result={asProblem(apply.error)} />
            </div>
          )}
        </Card>

        {catalog.isError ? (
          <Card>
            <Problem result={asProblem(catalog.error)} />
          </Card>
        ) : catalog.isLoading ? (
          <ContentSkeleton />
        ) : (
          <div className="flex flex-col md:flex-row gap-4">
            {/* The rail renders from the catalog alone, so it is present even
                before a region is configured — the shell is never blank.

                It scrolls itself and sticks: 10 groups of items are taller than
                most content panes, and a plain vertical stack made the WHOLE page
                scroll to accommodate a nav the reader was not reading. Its height
                is now its own problem, not the page's. */}
            <nav
              aria-label={i18nT('apps.agentcoreObservatory.page.rail_label')}
              className="md:w-56 shrink-0 md:sticky md:top-0 md:self-start md:max-h-[calc(100vh-13rem)] md:overflow-y-auto md:pr-1"
            >
              {groups.map((group) => (
                <div key={group.id} className="mb-3">
                  <div className="text-xs uppercase tracking-wide text-muted mb-1">
                    {labelFor(GROUP_LABEL_KEY, group.id)}
                  </div>
                  <ul>
                    {group.types.map((type) => (
                      <li key={type.id}>
                        <button
                          type="button"
                          onClick={() => setSelected(type.id)}
                          aria-current={selected === type.id ? 'true' : undefined}
                          className={`w-full text-left text-sm rounded px-2 py-1 truncate ${
                            selected === type.id
                              ? 'bg-accent/15 text-accent font-medium'
                              : 'hover:bg-bg-hover'
                          }`}
                        >
                          {labelFor(TYPE_LABEL_KEY, type.id)}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </nav>

            <div className="flex-1 min-w-0">
              {!cfg?.configured ? (
                <EmptyState
                  icon={<Telescope size={20} className="lucide-inline" />}
                  title={i18nT('apps.agentcoreObservatory.page.connect_first')}
                />
              ) : selectedType ? (
                <TypePane type={selectedType} />
              ) : (
                <EmptyState
                  icon={<Telescope size={20} className="lucide-inline" />}
                  title={i18nT('apps.agentcoreObservatory.page.pick_a_type')}
                />
              )}
            </div>
          </div>
        )}
      </div>
    </>
  )
}
