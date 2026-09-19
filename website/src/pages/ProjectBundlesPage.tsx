import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  ChevronRight,
  FolderGit2,
  FolderKanban,
  GitBranch,
  MessageSquare,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import Clickable from '../components/Clickable'
import { useConfirm } from '../components/ConfirmDialog'
import ErrorNotice from '../components/ErrorNotice'
import ProjectReviewDialog from '../components/ProjectReviewDialog'
import {
  Badge,
  Btn,
  Card,
  CardTitle,
  ContentSkeleton,
  EmptyState,
  Input,
  PageHeader,
  SendBtn,
} from '../components/ui'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { useAppDispatch } from '../store'
import { createSlot } from '../store/chatSlice'
import type {
  ProjectBundle,
  ProjectBundlesResponse,
} from '../types'
import { projectHealthBadge as healthBadge } from '../utils/projectHealth'

const PROJECTS_QUERY_KEY = ['project-bundles'] as const

/** The path of the Project's local copy — where `project.yaml` lives — as the
 *  Local copy card shows it. The newest registration is the one on disk here. */
function localCopyPath(project: ProjectBundle): string {
  return project.registrations[project.registrations.length - 1]?.path ?? project.id
}

/** The 'a declared repository is unavailable' notice, shared by the
 *  `sources_unavailable` state and the case where a missing secondary source
 *  rides along with `review_stale`. Leads with the edit to make, naming
 *  `project.yaml` by its full path (the UI never opens the file itself), then
 *  says what the pause costs; the synthesized source ids follow in monospace.
 *  It does NOT reuse the manifest-unavailable copy. */
function UnavailableSourcesNotice({ ids, manifestPath }: { ids: string[]; manifestPath: string }) {
  return (
    <>
      <ErrorNotice message={i18nT('pages.projectBundlesPage.sources_unavailable_help', { path: manifestPath.replace(/[\\/]+$/, '') })} askAgent />
      {ids.length ? (
        <ul className="mt-2 space-y-1">
          {ids.map(id => (
            <li className="break-all font-mono text-[12px] text-muted" key={id}>{id}</li>
          ))}
        </ul>
      ) : null}
    </>
  )
}

/** The removal that forgot the registration but left files behind. The
 *  Project is gone from the list by the time this renders, so it stands above
 *  the list (not in the vanished detail), names the Project, and lists every
 *  path the server could not delete inside the message — the hand-off carries
 *  them to the agent, and the owner can delete them by hand. Dismissable: a
 *  cleanup done by hand should not leave a stale alert. */
function CleanupPendingNotice({ name, paths, onDismiss }: { name: string; paths: string[]; onDismiss: () => void }) {
  return (
    <ErrorNotice
      title={name}
      message={[i18nT('pages.projectBundlesPage.remove_cleanup_pending'), ...paths].join('\n')}
      askAgent
      onDismiss={onDismiss}
      className="mb-3 max-w-4xl"
      testId="project-remove-cleanup-pending"
    />
  )
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div className="block text-[13px] text-muted">
      <label htmlFor={id}>{label}</label>
      {children}
    </div>
  )
}

function BundleForm({ mode, onClose }: { mode: 'create' | 'add'; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [source, setSource] = useState('')
  const mutation = useMutation({
    mutationFn: () => mode === 'create'
      ? api.createProjectBundle(name.trim(), path.trim())
      : api.addProjectBundle(source.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
      onClose()
    },
  })
  const canSubmit = mode === 'create'
    ? Boolean(name.trim() && path.trim())
    : Boolean(source.trim())

  return (
    <Card className="max-w-4xl">
      <CardTitle>
        {mode === 'create'
          ? i18nT('pages.projectBundlesPage.create_project')
          : i18nT('pages.projectBundlesPage.add_project')}
      </CardTitle>
      <p className="mb-4 text-[13px] text-muted">
        {mode === 'create'
          ? i18nT('pages.projectBundlesPage.create_project_help')
          : i18nT('pages.projectBundlesPage.add_project_help')}
      </p>
      <form className="space-y-3" onSubmit={event => { event.preventDefault(); if (canSubmit && !mutation.isPending) mutation.mutate() }}>
        {mode === 'create' ? (
          <>
            <Field id="project-bundle-name" label={i18nT('pages.projectBundlesPage.project_name')}>
              <Input id="project-bundle-name" name="project-name" autoComplete="off" className="mt-2 w-full" value={name} onChange={event => setName(event.target.value)} />
            </Field>
            <Field id="project-bundle-path" label={i18nT('pages.projectBundlesPage.bundle_folder')}>
              <Input id="project-bundle-path" name="bundle-path" autoComplete="off" className="mt-2 w-full font-mono" value={path} onChange={event => setPath(event.target.value)} />
            </Field>
          </>
        ) : (
          <Field id="project-bundle-source" label={i18nT('pages.projectBundlesPage.folder_or_git_url')}>
            <Input id="project-bundle-source" name="project-source" autoComplete="url" className="mt-2 w-full font-mono" value={source} onChange={event => setSource(event.target.value)} />
          </Field>
        )}
        {/* No hand-off: the create/add form's name, folder and source fields are
            unsaved drafts — navigating to the chat would discard them. */}
        <RequestError error={mutation.error} />
        <div className="flex flex-wrap gap-2">
          <SendBtn className="inline-flex items-center gap-1.5" type="submit" disabled={!canSubmit || mutation.isPending}>
            {mode === 'create'
              ? i18nT('pages.projectBundlesPage.create_project')
              : i18nT('pages.projectBundlesPage.add_project')}
          </SendBtn>
          <Btn type="button" onClick={onClose}>{i18nT('pages.projectBundlesPage.cancel')}</Btn>
        </div>
      </form>
    </Card>
  )
}

/** The failed request's parsed JSON body, when it carried one. Duck-typed on
 *  `body` (like `isReviewMovedError`) so it holds under a mocked `api/client`. */
function requestErrorBody(error: unknown): Record<string, unknown> | undefined {
  if (typeof error !== 'object' || error === null) return undefined
  const { body } = error as { body?: unknown }
  if (typeof body !== 'string' || !body.trim().startsWith('{')) return undefined
  try {
    const parsed: unknown = JSON.parse(body)
    return typeof parsed === 'object' && parsed !== null ? parsed as Record<string, unknown> : undefined
  } catch {
    return undefined
  }
}

/** The backend's machine-readable code on a failed request. Most handlers
 *  carry it as `code`; the sync refusals carry it as `error` (the body's
 *  other fields are the refusal's data), so both spellings are read. */
function requestErrorCode(error: unknown): string | undefined {
  const body = requestErrorBody(error)
  if (!body) return undefined
  if (typeof body.code === 'string' && body.code) return body.code
  return typeof body.error === 'string' && body.error ? body.error : undefined
}

/** A host that denies user namespaces cannot run the sandbox every Project
 *  Git operation goes through, so `add` and `sync` are refused with this code.
 *  There is no fallback to offer; the notice repeats the requirement. */
const SANDBOX_UNAVAILABLE = 'project_sandbox_unavailable'

/** A pull fast-forwards only: the bundle first, then EACH source on its own.
 *  When a checkout cannot be fast-forwarded (local commits, an unrelated
 *  history) or has uncommitted changes the merge would touch, the server
 *  refuses with this code and lists the checkouts that did move (`advanced`)
 *  beside the ones that did not (`diverged`, each with the state that blocked
 *  it); what advanced is never unwound, and nothing local is ever discarded.
 *  The owner resolves each diverged checkout in place and pulls again. */
const CHECKOUT_DIVERGED = 'project_checkout_diverged'

const CHECKOUT_DIVERGED_HELP: Record<string, string> = {
  'local-commits': 'pages.projectBundlesPage.checkout_diverged_local_commits',
  'dirty-tree': 'pages.projectBundlesPage.checkout_diverged_dirty_tree',
  'unrelated-history': 'pages.projectBundlesPage.checkout_diverged_unrelated_history',
}

type DivergedCheckout = { checkout: string; detail: string }

/** The refusal's `diverged` entries — the ONLY place a diverged checkout is
 *  read from. The body carries no single top-level `checkout`/`detail` pair;
 *  a key by either name outside the list is not a checkout and is not read.
 *  The literal `bundle` names the Project's own copy; anything else is a
 *  source id. */
function divergedCheckouts(body: Record<string, unknown>): DivergedCheckout[] {
  if (!Array.isArray(body.diverged)) return []
  return body.diverged.flatMap((entry: unknown): DivergedCheckout[] => {
    if (typeof entry !== 'object' || entry === null) return []
    const { checkout, detail } = entry as { checkout?: unknown; detail?: unknown }
    return [{
      checkout: typeof checkout === 'string' && checkout ? checkout : 'bundle',
      detail: typeof detail === 'string' ? detail : '',
    }]
  })
}

/** Where a diverged checkout is, as the notice shows it: `bundle` is the
 *  Project's own copy — shown as the Local copy card's path, the place the
 *  owner resolves it — and a source id is shown as itself. */
function divergedCheckoutPath(checkout: string, project: ProjectBundle | undefined): string {
  if (checkout === 'bundle' && project) return localCopyPath(project)
  return checkout
}

function checkoutDivergedMessage(body: Record<string, unknown>, project: ProjectBundle | undefined): string {
  const diverged = divergedCheckouts(body)
  const lines = diverged.length
    ? diverged.map(({ checkout, detail }) => {
      // An unknown detail is still a diverged checkout: the generic line says
      // so and names the checkout, rather than falling back to the raw message.
      const key = CHECKOUT_DIVERGED_HELP[detail] ?? 'pages.projectBundlesPage.checkout_diverged'
      return i18nT(key, { checkout: divergedCheckoutPath(checkout, project) })
    })
    // A refusal that names no checkout still refused: the generic line, on
    // the Project's own copy.
    : [i18nT('pages.projectBundlesPage.checkout_diverged', { checkout: divergedCheckoutPath('bundle', project) })]
  const advanced = Array.isArray(body.advanced) ? body.advanced.filter((entry): entry is string => typeof entry === 'string' && entry !== '') : []
  // What DID move is said last, after the refusals it stands beside: the
  // owner reads why the pull stopped, then what it already brought in.
  if (advanced.length) lines.push(i18nT('pages.projectBundlesPage.checkout_advanced', { checkouts: advanced.join(', ') }))
  return lines.join('\n\n')
}

/** The checkout's own `project.yaml` does not parse, so the request that
 *  re-reads it refuses with this code and the parser's message as `detail`.
 *  Nothing in the dashboard can repair the file; the notice hands over the
 *  reason and where the file is. */
const MANIFEST_INVALID = 'project_manifest_invalid'

function manifestInvalidMessage(body: Record<string, unknown>, project: ProjectBundle | undefined): string {
  // The parser's text is the reason, shown as received; a body without one
  // still names the file to fix.
  const detail = typeof body.detail === 'string' && body.detail.trim() ? body.detail.trim() : i18nT('pages.projectBundlesPage.project_request_failed')
  const path = project ? localCopyPath(project).replace(/[\\/]+$/, '') : ''
  return i18nT('pages.projectBundlesPage.manifest_invalid', { detail, path })
}

function requestErrorMessage(error: unknown, project?: ProjectBundle): string | null {
  if (!error) return null
  const code = requestErrorCode(error)
  if (code === SANDBOX_UNAVAILABLE) return i18nT('pages.projectBundlesPage.sandbox_unavailable_help')
  if (code === CHECKOUT_DIVERGED) return checkoutDivergedMessage(requestErrorBody(error) ?? {}, project)
  if (code === MANIFEST_INVALID) return manifestInvalidMessage(requestErrorBody(error) ?? {}, project)
  return error instanceof Error && error.message
    ? error.message
    : i18nT('pages.projectBundlesPage.project_request_failed')
}

/** The bold lead a refusal carries above its message, when it has one: the
 *  manifest refusal leads with what could not be read, so the parser's text
 *  under it reads as the reason and not as the whole story. */
function requestErrorTitle(error: unknown): string | undefined {
  if (requestErrorCode(error) === MANIFEST_INVALID) return i18nT('pages.projectBundlesPage.manifest_invalid_title')
  return undefined
}

/**
 * A failed Project request, rendered through `ErrorNotice` so the structured
 * context (endpoint, status, backend code) survives. `askAgent` is decided by
 * the call site: on where the surface holds nothing unsaved, off beside a draft.
 * A sandbox refusal is the one exception: it is a host requirement the form
 * cannot satisfy, so the hand-off is offered there too — the draft it would
 * discard is a single path or URL the owner can retype. `project` lets a
 * refusal that names a checkout show where it is.
 */
function RequestError({ error, askAgent = false, className, project }: { error: unknown; askAgent?: boolean; className?: string; project?: ProjectBundle }) {
  const sandbox = requestErrorCode(error) === SANDBOX_UNAVAILABLE
  return <ErrorNotice title={requestErrorTitle(error)} message={requestErrorMessage(error, project)} askAgent={askAgent || sandbox} className={className} />
}

/** Which "Pull updates" control the owner clicked, so the outcome (the pulled
 *  line or the refusal) renders beside THAT control rather than only in the
 *  Local copy card: a pull started from the review-stale banner answers in the
 *  banner. */
type SyncSource = 'review' | 'sources' | 'unavailable' | 'local'

/** What a completed pull left to say. `unavailableSources` is the declared
 *  source ids the pull could not fetch: empty means every checkout followed
 *  and the outcome is the plain "Updates pulled." line; non-empty means the
 *  bundle (and every source that could) fast-forwarded while these did not, so
 *  the outcome names them rather than reading as a success. */
type Pulled = { unavailableSources: string[] }

/** What a completed removal leaves for the list to say: the registration is
 *  gone either way; `paths` is what the server could not delete afterwards. */
type ProjectRemoved = { name: string; cleanupPending: string[] }

function ProjectDetails({ project, onBack, onRemoved }: { project: ProjectBundle; onBack: () => void; onRemoved: (removed: ProjectRemoved) => void }) {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const [pulled, setPulled] = useState<Pulled | null>(null)
  const [syncSource, setSyncSource] = useState<SyncSource | null>(null)
  const repositories = project.sources.filter(source => source.type === 'repo')
  const { confirm, confirmDialog } = useConfirm()
  const sessionMutation = useMutation({
    mutationFn: () => dispatch(createSlot({ project_id: project.id })).unwrap(),
    onSuccess: slot => navigate(`/chat?sid=${encodeURIComponent(slot.key)}`),
  })
  const syncMutation = useMutation({
    mutationFn: () => api.syncProjectBundle(project.id),
    onSuccess: async result => {
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
      // Read defensively: a partially mocked client, or a response without the
      // key, is a pull where every source followed.
      const unavailable = Array.isArray(result?.unavailable_sources)
        ? result.unavailable_sources.filter((id): id is string => typeof id === 'string' && id !== '')
        : []
      setPulled({ unavailableSources: unavailable })
    },
  })
  const removeMutation = useMutation({
    mutationFn: () => api.removeProjectBundle(project.id),
    // Every 200 means the registration is gone, so the detail leaves; what the
    // server could not delete afterwards is reported where the list is.
    onSuccess: async result => {
      onRemoved({ name: project.name, cleanupPending: Array.isArray(result.cleanup_pending) ? result.cleanup_pending : [] })
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
    },
  })
  const [reviewOpen, setReviewOpen] = useState(false)
  const syncable = project.registrations.some(registration => registration.syncable)

  /** The page's one pull action, started from `source`: the outcome is then
   *  anchored to that control. A new click clears the previous outcome, and
   *  the mutation clears its own error when it runs again. */
  function syncFrom(source: SyncSource) {
    setPulled(null)
    setSyncSource(source)
    syncMutation.mutate()
  }
  const syncButton = (source: SyncSource, label: string) => (
    <Btn disabled={syncMutation.isPending} onClick={() => syncFrom(source)}>
      <RefreshCw className="lucide-inline" />
      {label}
    </Btn>
  )
  /** The pull's outcome — the "Updates pulled." line, the partial-pull notice
   *  naming the sources that could not be fetched, or the refusal as an error
   *  notice — rendered only under the control that started it. A partial pull
   *  is an error the owner acts on (the ids are what to fix in project.yaml),
   *  so it takes the shared error surface with the hand-off, never the plain
   *  success line. */
  const syncOutcome = (source: SyncSource) => syncSource === source ? (
    <div data-testid={`project-sync-outcome-${source}`}>
      {pulled && pulled.unavailableSources.length === 0 && (
        <div className="mt-3 text-[13px] text-ok" role="status" aria-live="polite">{i18nT('pages.projectBundlesPage.updates_pulled')}</div>
      )}
      {pulled && pulled.unavailableSources.length > 0 && (
        <ErrorNotice
          message={i18nT('pages.projectBundlesPage.updates_pulled_partial', { count: pulled.unavailableSources.length, ids: pulled.unavailableSources.join(', ') })}
          askAgent
          className="mt-3"
          testId={`project-sync-partial-${source}`}
        />
      )}
      <RequestError error={syncMutation.error} askAgent className="mt-3" project={project} />
    </div>
  ) : null

  async function removeProject() {
    // The confirm repeats the trigger's own label: the button that opened
    // this dialog reads "Remove from …", and the action that completes it
    // reads the same, so the owner confirms the thing they clicked.
    const accepted = await confirm({
      title: i18nT('pages.projectBundlesPage.remove_project_title', { name: project.name }),
      body: i18nT('pages.projectBundlesPage.remove_project_body'),
      confirmLabel: i18nT('pages.projectBundlesPage.remove_from_kiro_crew'),
    })
    if (accepted) removeMutation.mutate()
  }

  return (
    <div className="max-w-4xl">
      <Btn className="mb-3" onClick={onBack}>
        <ArrowLeft className="lucide-inline" />
        {i18nT('pages.projectBundlesPage.back_to_projects')}
      </Btn>
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-semibold text-text-strong">{project.name}</h2>
              <Badge variant={healthBadge(project.health.status).variant}>
                {healthBadge(project.health.status).label}
              </Badge>
            </div>
            <p className="max-w-2xl text-sm text-muted">{project.description || i18nT('pages.projectBundlesPage.no_description')}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {/* The same term the rest of the dashboard uses for creating a
                slot ("New chat" in the sidebar and its create menu), so the
                Project page does not name the one action twice. */}
            <SendBtn className="inline-flex items-center gap-1.5" disabled={sessionMutation.isPending || project.health.status !== 'healthy'} onClick={() => sessionMutation.mutate()}>
              <MessageSquare className="lucide-inline" />
              {i18nT('pages.projectBundlesPage.new_chat')}
            </SendBtn>
          </div>
        </div>
        {project.health.status === 'review_stale' ? (
          <div className="mt-4">
            <ErrorNotice message={i18nT('pages.projectBundlesPage.review_stale_help')} askAgent />
            {project.health.stale_files?.length ? (
              <ul className="mt-2 space-y-1">
                {project.health.stale_files.map(file => (
                  <li className="break-all font-mono text-[12px] text-muted" key={file}>{file}</li>
                ))}
              </ul>
            ) : null}
            <div className="mt-2 flex flex-wrap gap-2">
              {/* Opens the digest-bound review: the files' content is shown
                  and accepted in the dialog, never from this button alone. */}
              <Btn onClick={() => setReviewOpen(true)}>
                <ShieldCheck className="lucide-inline" />
                {i18nT('pages.projectBundlesPage.review_changes')}
              </Btn>
              {/* Every unreadable entry's remedy ends in "pull updates", so
                  the pull stands beside the review it feeds. */}
              {syncable && syncButton('review', i18nT('pages.projectBundlesPage.pull_updates'))}
            </div>
            {syncOutcome('review')}
            {/* A stale digest outranks a missing SECONDARY source, so the two
                states co-occur — show the sources notice as well. */}
            {project.health.unavailable_sources?.length ? (
              <div className="mt-4">
                <UnavailableSourcesNotice ids={project.health.unavailable_sources} manifestPath={localCopyPath(project)} />
              </div>
            ) : null}
          </div>
        ) : project.health.status === 'sources_unavailable' ? (
          <div className="mt-4">
            <UnavailableSourcesNotice ids={project.health.unavailable_sources ?? []} manifestPath={localCopyPath(project)} />
            {syncable && (
              <div className="mt-2 flex flex-wrap gap-2">
                {syncButton('sources', i18nT('pages.projectBundlesPage.pull_updates'))}
              </div>
            )}
            {syncOutcome('sources')}
          </div>
        ) : project.health.status !== 'healthy' ? (
          <div className="mt-4">
            <ErrorNotice message={i18nT('pages.projectBundlesPage.manifest_unavailable_help')} askAgent />
            {syncable && (
              <div className="mt-2 flex flex-wrap gap-2">
                {syncButton('unavailable', i18nT('pages.projectBundlesPage.pull_updates'))}
              </div>
            )}
            {syncOutcome('unavailable')}
          </div>
        ) : null}
        <RequestError error={sessionMutation.error} askAgent className="mt-4" />
      </Card>

      <Card>
        <CardTitle>
          {i18nT('pages.projectBundlesPage.sessions')}
          <Badge variant="muted">{fmtNumber(project.sessions?.length ?? 0)}</Badge>
        </CardTitle>
        {project.sessions?.length ? (
          <div className="space-y-2">
            {project.sessions.map(session => (
              <Link className="flex min-w-0 items-center justify-between gap-3 rounded-md border border-border bg-bg-elevated px-3 py-2 transition-colors hover:border-accent hover:bg-bg-hover" key={session.key} to={`/chat?sid=${encodeURIComponent(session.key)}`}>
                <span className="truncate text-sm text-text">{session.title}</span>
                <span className="shrink-0 text-[12px] text-muted">{i18nT('pages.projectBundlesPage.message_count', { count: session.messages })}</span>
              </Link>
            ))}
          </div>
        ) : <div className="text-[13px] text-muted">{i18nT('pages.projectBundlesPage.no_sessions')}</div>}
      </Card>

      <Card>
        <CardTitle>{i18nT('pages.projectBundlesPage.overview')}</CardTitle>
        <div className="space-y-3 text-[13px]">
          <div>
            <div className="text-muted">{i18nT('pages.projectBundlesPage.working_repository')}</div>
            <div className="mt-1 font-mono text-text">{project.workspace_source === 'self' ? i18nT('pages.projectBundlesPage.project_bundle') : project.workspace_source}</div>
          </div>
        </div>
      </Card>

      <Card>
        <CardTitle>
          <GitBranch className="lucide-inline" />
          {i18nT('pages.projectBundlesPage.repositories')}
          <Badge variant="muted">{fmtNumber(repositories.length)}</Badge>
        </CardTitle>
        {repositories.length ? (
          <div className="space-y-2">
            {repositories.map(source => {
              // The health banner names the failing ids; the row they describe
              // must not read as healthy beside it, and it wears the SAME term
              // the banner's badge does ("Source unavailable"), not a second one.
              const unavailable = source.status === 'unavailable' || Boolean(project.health.unavailable_sources?.includes(source.id))
              // Declared but not yet cloned: the clone waits on the review that
              // names this source, so the row says so and what lifts it.
              const pending = !unavailable && source.status === 'pending'
              return (
                <div className="rounded-md border border-border bg-bg-elevated px-3 py-3" data-testid={`project-source-${source.id}`} key={source.id}>
                  <div className="flex flex-wrap items-center gap-2 font-medium text-text">
                    {source.id}
                    {unavailable && <Badge variant="err">{i18nT('pages.projectBundlesPage.source_unavailable')}</Badge>}
                    {pending && <Badge variant="muted">{i18nT('pages.projectBundlesPage.pending_review')}</Badge>}
                  </div>
                  {typeof source.url === 'string' && <div className="mt-1 break-all font-mono text-[13px] text-muted">{source.url}</div>}
                  {typeof source.default_branch === 'string' && source.default_branch && <div className="mt-1 text-[13px] text-muted">{i18nT('pages.projectBundlesPage.default_branch')}: <span className="font-mono text-text">{source.default_branch}</span></div>}
                  {pending && <div className="mt-1 text-[13px] text-muted">{i18nT('pages.projectBundlesPage.pending_review_help')}</div>}
                </div>
              )
            })}
          </div>
        ) : <div className="text-[13px] text-muted">{i18nT('pages.projectBundlesPage.no_sources')}</div>}
      </Card>

      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <CardTitle className="mb-0">{i18nT('pages.projectBundlesPage.local_copy')}</CardTitle>
          {syncable && syncButton('local', i18nT('pages.projectBundlesPage.pull_updates'))}
        </div>
        <div className="mt-3 space-y-2">
          <div>
            <div className="text-[12px] text-muted">{i18nT('pages.projectBundlesPage.project_id')}</div>
            <div className="mt-1 break-all font-mono text-[13px] text-text">{project.id}</div>
          </div>
          {project.registrations.map(registration => (
            <div className="rounded-md border border-border bg-bg-elevated px-3 py-2" key={`${registration.origin}:${registration.path}`}>
              <div className="text-[12px] text-muted">
                {registration.syncable
                  ? i18nT('pages.projectBundlesPage.shared_with_git')
                  : i18nT('pages.projectBundlesPage.local_project')}
              </div>
              <div className="mt-1 break-all font-mono text-[13px] text-text">{registration.path}</div>
            </div>
          ))}
        </div>
        {syncOutcome('local')}
        <div className="mt-4 border-t border-border pt-4">
          <Btn danger disabled={removeMutation.isPending} onClick={() => { void removeProject() }}>
            <Trash2 className="lucide-inline" />
            {i18nT('pages.projectBundlesPage.remove_from_kiro_crew')}
          </Btn>
          <RequestError error={removeMutation.error} askAgent className="mt-3" />
        </div>
      </Card>
      {confirmDialog}
      <ProjectReviewDialog project={project} open={reviewOpen} onClose={() => setReviewOpen(false)} />
    </div>
  )
}

function ProjectList({ projects, onOpen }: { projects: ProjectBundle[]; onOpen: (id: string) => void }) {
  return (
    <div className="max-w-4xl space-y-3">
      {projects.map(project => (
        <Clickable
          aria-label={`${i18nT('pages.projectBundlesPage.open_project', { name: project.name })} — ${project.registrations[project.registrations.length - 1]?.path ?? project.id}`}
          className="group flex min-w-0 items-center justify-between gap-4 rounded-lg border border-border bg-card px-4 py-4 shadow-sm transition-colors hover:border-accent hover:bg-bg-hover"
          data-project-id={project.id}
          key={project.id}
          onClick={() => onOpen(project.id)}
        >
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <div className="truncate text-base font-semibold text-text-strong">{project.name}</div>
              <Badge variant={healthBadge(project.health.status).variant}>
                {healthBadge(project.health.status).label}
              </Badge>
            </div>
            <div className="mt-1 line-clamp-2 text-[13px] text-muted">{project.description || i18nT('pages.projectBundlesPage.no_description')}</div>
            <div className="mt-1 truncate font-mono text-[12px] text-muted">{project.registrations[project.registrations.length - 1]?.path ?? project.id}</div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
              <span className="inline-flex items-center gap-1.5">
                {i18nT('pages.projectBundlesPage.repositories')}
                <Badge variant="muted">{fmtNumber(project.sources.filter(source => source.type === 'repo').length)}</Badge>
              </span>
              <span className="inline-flex items-center gap-1.5">
                {i18nT('pages.projectBundlesPage.sessions')}
                <Badge variant="muted">{fmtNumber(project.sessions?.length ?? 0)}</Badge>
              </span>
              <span>{project.registrations.some(registration => registration.origin === 'managed_git') ? i18nT('pages.projectBundlesPage.shared_with_git') : i18nT('pages.projectBundlesPage.local_project')}</span>
            </div>
          </div>
          <ChevronRight className="lucide-inline shrink-0 text-muted transition-colors group-hover:text-text" />
        </Clickable>
      ))}
    </div>
  )
}

function ProjectBundlesContent({ embedded }: { embedded: boolean }) {
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('project')
  const view = params.get('view')
  const form = view === 'create' || view === 'add' ? view : null
  const projectsQuery = useQuery<ProjectBundlesResponse>({
    queryKey: PROJECTS_QUERY_KEY,
    queryFn: () => api.projectBundles(),
  })
  const projects = projectsQuery.data?.projects ?? []
  const selected = selectedId ? projects.find(project => project.id === selectedId) : undefined
  /** The last removal that left files behind, shown above the list until the
   *  owner dismisses it. A clean removal sets nothing. */
  const [cleanupPending, setCleanupPending] = useState<ProjectRemoved | null>(null)
  const updateRoute = (changes: { project?: string | null; view?: string | null }, replace = false) => {
    setParams(current => {
      const next = new URLSearchParams(current)
      for (const [key, value] of Object.entries(changes)) {
        if (value) next.set(key, value)
        else next.delete(key)
      }
      return next
    }, { replace })
  }
  useEffect(() => {
    if (projectsQuery.isLoading || !selectedId || selected) return
    setParams(current => {
      const next = new URLSearchParams(current)
      next.delete('project')
      next.delete('view')
      return next
    }, { replace: true })
  }, [projectsQuery.isLoading, selected, selectedId, setParams])
  const actions = !selected ? (
    <>
      <Btn onClick={() => updateRoute({ project: null, view: 'add' })}>
        <FolderGit2 className="lucide-inline" />
        {i18nT('pages.projectBundlesPage.add_project')}
      </Btn>
      <SendBtn className="inline-flex items-center gap-1.5" onClick={() => updateRoute({ project: null, view: 'create' })}>
        <Plus className="lucide-inline" />
        {i18nT('pages.projectBundlesPage.create_project')}
      </SendBtn>
    </>
  ) : null

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.projectBundlesPage.projects')} subtitle={i18nT('pages.projectBundlesPage.subtitle')} actions={actions} />}
      <div className={embedded ? 'pb-8' : 'px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0'}>
        {embedded && actions && <div className="mb-3 flex flex-wrap justify-end gap-2">{actions}</div>}
        {cleanupPending && !selected && (
          <CleanupPendingNotice name={cleanupPending.name} paths={cleanupPending.cleanupPending} onDismiss={() => setCleanupPending(null)} />
        )}
        {form && !selected && <BundleForm mode={form} onClose={() => updateRoute({ view: null })} />}
        {projectsQuery.isLoading ? (
          <Card className="max-w-4xl"><ContentSkeleton rows={4} /></Card>
        ) : projectsQuery.error ? (
          <Card className="max-w-4xl"><ErrorNotice message={i18nT('pages.projectBundlesPage.failed_to_load_projects')} askAgent /></Card>
        ) : projects.length === 0 ? (
          <Card className="max-w-4xl">
            <EmptyState icon={<FolderKanban className="lucide-inline" />} title={i18nT('pages.projectBundlesPage.no_projects_yet')} subtitle={i18nT('pages.projectBundlesPage.empty_subtitle')} testId="project-bundles-empty" />
          </Card>
        ) : selected ? (
          <ProjectDetails
            key={selected.id}
            project={selected}
            onBack={() => updateRoute({ project: null, view: null })}
            onRemoved={removed => {
              setCleanupPending(removed.cleanupPending.length ? removed : null)
              updateRoute({ project: null, view: null })
            }}
          />
        ) : (
          <ProjectList projects={projects} onOpen={id => updateRoute({ project: id, view: null })} />
        )}
      </div>
    </>
  )
}

export default function ProjectBundlesPage({ embedded = false }: { embedded?: boolean }) {
  return <ProjectBundlesContent embedded={embedded} />
}
