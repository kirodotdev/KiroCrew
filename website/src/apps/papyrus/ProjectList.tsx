/**
 * ProjectList — the Papyrus landing view: create a paper, clone one, or open one.
 *
 * This is the view that carries the page-layout pattern (`PageHeader` +
 * `px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0` container + a `StatCard` row +
 * `Card`/`CardTitle` sections), because it is the page-shaped half of the app. The
 * editor is a full-bleed split pane by necessity — a paper and its PDF need the
 * whole viewport — and it carries its own toolbar instead.
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Download, FileCheck2, FilePlus2, GitBranch, Loader2, ScrollText, Trash2, X } from 'lucide-react'
import { Btn, Card, CardTitle, ContentSkeleton, EmptyState, Input, PageHeader, SendBtn, StatCard } from '../../components/ui'
import Clickable from '../../components/Clickable'
import InfoTip from '../../components/InfoTip'
import { fmtBytes, fmtDateTime } from '../../i18n/format'
import { papyrusApi, type Project } from './api'
import { pruneSlots } from './lib'

import { i18nT } from '../../i18n/t'

export interface ProjectListProps {
  onOpenProject: (name: string) => void
}

const PROJECTS_KEY = ['papyrus', 'projects']
const HEALTH_KEY = ['papyrus', 'health']

/** How often /health is re-read while a compiler install is running. */
const HEALTH_POLL_MS = 2000

/** Render an epoch-SECONDS timestamp in the APP's language.
 *
 * `fmtDateTime`, not `toLocaleString`: the paper list sits inside a translated UI,
 * so an English date next to translated column headers is the bug this avoids. */
function formatModified(epochSeconds: number): string {
  return fmtDateTime(epochSeconds * 1000)
}

export default function ProjectList({ onOpenProject }: ProjectListProps) {
  const queryClient = useQueryClient()
  const [newName, setNewName] = useState('')
  const [cloneUrl, setCloneUrl] = useState('')
  const [error, setError] = useState('')

  const projectsQuery = useQuery({
    queryKey: PROJECTS_KEY,
    queryFn: () => papyrusApi.listProjects(),
  })
  const healthQuery = useQuery({
    queryKey: HEALTH_KEY,
    queryFn: () => papyrusApi.health(),
    staleTime: 60_000,
  })

  const projects: Project[] = useMemo(
    () => (Array.isArray(projectsQuery.data?.projects) ? projectsQuery.data.projects : []),
    [projectsQuery.data],
  )
  // Stale chat-slot keys would otherwise resurrect a deleted paper's conversation
  // if its name were reused. Pruned whenever the authoritative list lands.
  //
  // MUST be an effect gated on the query having resolved, not a `useMemo`. A
  // render-phase prune runs on the FIRST render too, when `data` is still
  // undefined and `projects` is therefore `[]` — which reads as "no papers
  // exist" and deletes every live paper's slot binding, orphaning the very
  // conversations this is meant to protect.
  useEffect(() => {
    if (!projectsQuery.data) return
    pruneSlots(projects.map(p => p.name))
  }, [projects, projectsQuery.data])

  const compiledCount = projects.filter(p => p.has_pdf).length
  const compiler = healthQuery.data?.compiler ?? ''
  const managed = healthQuery.data?.managed
  // The job is only "in flight" for the three working states; `idle` means nothing
  // has been started and `error`/`done` are terminal, so neither should keep the
  // button spinning.
  const provisioning =
    managed?.job.state === 'downloading' ||
    managed?.job.state === 'verifying' ||
    managed?.job.state === 'installing'

  const invalidate = () => queryClient.invalidateQueries({ queryKey: PROJECTS_KEY })

  // Poll /health while a provisioning job runs so the banner narrates progress and
  // flips to the installed compiler on its own. Bounded to the running states: a
  // steady 2s poll of a filesystem probe would otherwise never stop.
  useEffect(() => {
    if (!provisioning) return
    const timer = setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: HEALTH_KEY })
    }, HEALTH_POLL_MS)
    return () => clearInterval(timer)
  }, [provisioning, queryClient])

  const provisionMutation = useMutation({
    mutationFn: () => papyrusApi.provisionCompiler(),
    onSuccess: () => {
      setError('')
      // Refetch at once so `job.state` turns the poll above on without waiting a
      // whole interval for the first tick.
      void queryClient.invalidateQueries({ queryKey: HEALTH_KEY })
    },
    onError: (err: Error) => setError(err.message),
  })

  const createMutation = useMutation({
    mutationFn: (name: string) => papyrusApi.createProject(name),
    onSuccess: async (result) => {
      setNewName('')
      setError('')
      await invalidate()
      onOpenProject(result.name)
    },
    onError: (err: Error) => setError(err.message),
  })

  const cloneMutation = useMutation({
    mutationFn: (url: string) => papyrusApi.cloneProject(url),
    onSuccess: async (result) => {
      setCloneUrl('')
      setError('')
      await invalidate()
      onOpenProject(result.name)
    },
    onError: (err: Error) => setError(err.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (name: string) => papyrusApi.deleteProject(name),
    onSuccess: () => invalidate(),
    onError: (err: Error) => setError(err.message),
  })

  const submitCreate = () => {
    const name = newName.trim()
    if (name) createMutation.mutate(name)
  }
  const submitClone = () => {
    const url = cloneUrl.trim()
    if (url) cloneMutation.mutate(url)
  }

  return (
    <>
      <PageHeader
        title={i18nT('apps.papyrus.page.papyrus')}
        subtitle={i18nT('apps.papyrus.page.latex_papers_with_a_live_pdf_preview')}
      />
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard
            label={i18nT('apps.papyrus.page.papers')}
            value={projectsQuery.isLoading ? undefined : projects.length}
            accent
          />
          <StatCard
            label={i18nT('apps.papyrus.page.compiled_count_label')}
            value={projectsQuery.isLoading ? undefined : compiledCount}
          />
          <StatCard
            label={i18nT('apps.papyrus.page.compiler')}
            value={healthQuery.isLoading ? undefined : compiler || i18nT('apps.papyrus.page.none_found')}
            colorClass={compiler ? undefined : 'text-warn'}
            title={i18nT('apps.papyrus.page.compiler_tip')}
          />
        </div>

        {!healthQuery.isLoading && !compiler && (
          <div className="mb-4 bg-warn/10 border border-warn/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
            <AlertTriangle className="lucide-inline text-warn shrink-0 mt-0.5" />
            <div className="flex-1 text-[13px] text-text">
              {/* Offer the one-click managed install where a pinned build exists;
                  elsewhere (and on a failed job) keep the manual advice, which is
                  still the only route on an unsupported platform. */}
              {managed?.supported
                ? i18nT('apps.papyrus.page.no_compiler_install_bundled_prompt')
                : i18nT('apps.papyrus.page.no_compiler_found_install_texlive_or_tectonic')}
              {/* A determinate line for a multi-MB download. The job already
                  reports `bytes_downloaded`/`bytes_total` and nothing rendered
                  them, so a 22MB fetch on a slow link was an indefinite spinner
                  labelled "Installing…" with no way to tell progress from a hang.
                  `verifying`/`installing` get their own words for the same reason —
                  all three states read identically before.
                  Sizes go through `fmtBytes` (the i18n seam), never `toFixed`. */}
              {provisioning && (
                <div className="mt-1 text-muted">
                  {managed?.job.state === 'verifying'
                    ? i18nT('apps.papyrus.page.verifying_compiler')
                    : managed && managed.job.bytes_total > 0
                      ? i18nT('apps.papyrus.page.downloading_compiler_progress', {
                          done: fmtBytes(managed.job.bytes_downloaded),
                          total: fmtBytes(managed.job.bytes_total),
                        })
                      : i18nT('apps.papyrus.page.installing_compiler')}
                </div>
              )}
              {managed?.job.state === 'error' && managed.job.error && (
                <div className="mt-1 text-muted break-words">{managed.job.error}</div>
              )}
            </div>
            {managed?.supported && (
              <Btn
                primary
                className="shrink-0"
                onClick={() => provisionMutation.mutate()}
                disabled={provisioning || provisionMutation.isPending}
              >
                {provisioning ? (
                  <>
                    <Loader2 className="lucide-inline animate-spin" />
                    {i18nT('apps.papyrus.page.installing_compiler')}
                  </>
                ) : (
                  <>
                    <Download className="lucide-inline" />
                    {i18nT('apps.papyrus.page.install_compiler')}
                  </>
                )}
              </Btn>
            )}
          </div>
        )}

        {error && (
          <div
            className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise"
            role="alert"
          >
            <AlertTriangle className="lucide-inline text-danger shrink-0 mt-0.5" />
            <div className="flex-1 text-[13px] text-text break-words">{error}</div>
            <button
              type="button"
              onClick={() => setError('')}
              aria-label={i18nT('apps.papyrus.page.dismiss_error')}
              className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors"
            >
              <X className="lucide-inline" />
            </button>
          </div>
        )}

        <Card>
          <CardTitle>
            {i18nT('apps.papyrus.page.start_a_paper')}
            <InfoTip text={i18nT('apps.papyrus.page.start_a_paper_tip')} />
          </CardTitle>
          <div className="flex flex-wrap gap-2 mb-3">
            <Input
              className="flex-1 min-w-[14rem]"
              aria-label={i18nT('apps.papyrus.page.new_paper_name')}
              placeholder={i18nT('apps.papyrus.page.new_paper_name_placeholder')}
              value={newName}
              onChange={e => setNewName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') submitCreate() }}
            />
            <SendBtn onClick={submitCreate} disabled={!newName.trim() || createMutation.isPending}>
              {createMutation.isPending
                ? <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
                : <FilePlus2 className="lucide-inline" />}
              {i18nT('apps.papyrus.page.create')}
            </SendBtn>
          </div>
          <div className="flex flex-wrap gap-2">
            <Input
              className="flex-1 min-w-[14rem]"
              aria-label={i18nT('apps.papyrus.page.clone_url')}
              placeholder={i18nT('apps.papyrus.page.clone_url_placeholder')}
              value={cloneUrl}
              onChange={e => setCloneUrl(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') submitClone() }}
            />
            <Btn onClick={submitClone} disabled={!cloneUrl.trim() || cloneMutation.isPending}>
              {cloneMutation.isPending
                ? <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
                : <GitBranch className="lucide-inline" />}
              {cloneMutation.isPending
                ? i18nT('apps.papyrus.page.cloning')
                : i18nT('apps.papyrus.page.clone')}
            </Btn>
          </div>
        </Card>

        <Card>
          <CardTitle>{i18nT('apps.papyrus.page.your_papers')}</CardTitle>
          {/* `isLoading` first: `projects` is `[]` while the query is in flight, so a
              bare length check told every returning user "No papers yet" for the
              duration of the fetch — the one message guaranteed to be wrong for them.
              The StatCards above already gate on this same flag. */}
          {projectsQuery.isLoading ? (
            <ContentSkeleton rows={3} />
          ) : projects.length === 0 ? (
            <EmptyState
              icon={<ScrollText className="lucide-inline" />}
              title={i18nT('apps.papyrus.page.no_papers_yet')}
              subtitle={i18nT('apps.papyrus.page.create_one_above_or_clone_an_existing_repository')}
            />
          ) : (
            <table className="w-full border-collapse table-striped">
              <thead>
                <tr>
                  <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
                    {i18nT('apps.papyrus.page.paper')}
                  </th>
                  <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
                    {i18nT('apps.papyrus.page.last_edited')}
                  </th>
                  <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
                    {i18nT('apps.papyrus.page.pdf')}
                  </th>
                  <th className="px-2.5 py-2 border-b border-border">
                    <span className="sr-only">{i18nT('apps.papyrus.page.actions')}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {projects.map(project => (
                  <tr key={project.name}>
                    <td className="px-2.5 py-2">
                      <Clickable
                        onClick={() => onOpenProject(project.name)}
                        className="inline-flex items-center gap-1.5 text-[13px] text-text-strong font-medium cursor-pointer hover:text-accent focus-ring rounded"
                      >
                        <ScrollText className="lucide-inline shrink-0 opacity-70" />
                        {project.name}
                      </Clickable>
                    </td>
                    <td className="px-2.5 py-2 text-[12px] text-muted">
                      {formatModified(project.modified)}
                    </td>
                    <td className="px-2.5 py-2">
                      {project.has_pdf ? (
                        <span className="inline-flex items-center gap-1 text-[12px] text-ok">
                          <FileCheck2 className="lucide-inline" />
                          {i18nT('apps.papyrus.page.pdf_ready_status')}
                        </span>
                      ) : (
                        <span className="text-[12px] text-muted">
                          {i18nT('apps.papyrus.page.pdf_missing_status')}
                        </span>
                      )}
                    </td>
                    <td className="px-2.5 py-2 text-right">
                      <button
                        type="button"
                        aria-label={i18nT('apps.papyrus.page.delete_paper', { name: project.name })}
                        title={i18nT('apps.papyrus.page.delete_paper', { name: project.name })}
                        onClick={() => {
                          // Deleting a paper is `rmtree` on the server with no
                          // undo, including uncommitted work — so it confirms,
                          // exactly as deleting a single file already does.
                          if (
                            window.confirm(
                              i18nT('apps.papyrus.page.delete_paper_confirm', {
                                name: project.name,
                              }),
                            )
                          ) {
                            deleteMutation.mutate(project.name)
                          }
                        }}
                        disabled={deleteMutation.isPending}
                        className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer bg-transparent border-none transition-colors disabled:opacity-40"
                      >
                        <Trash2 className="lucide-inline" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </>
  )
}
