import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Bot,
  ChevronRight,
  FolderGit2,
  FolderKanban,
  GitBranch,
  MessageSquare,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
  ShieldOff,
  Sparkles,
  Trash2,
} from 'lucide-react'
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import Clickable from '../components/Clickable'
import { useConfirm } from '../components/ConfirmDialog'
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

const PROJECTS_QUERY_KEY = ['project-bundles'] as const

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
        {mutation.error && <RequestError error={mutation.error} />}
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

function RequestError({ error }: { error: unknown }) {
  return (
    <div className="rounded-md border border-danger/40 bg-danger-subtle px-3 py-2 text-[13px] text-danger" role="alert">
      {error instanceof Error
        ? error.message
        : i18nT('pages.projectBundlesPage.project_request_failed')}
    </div>
  )
}

function ProjectDetails({ project, onBack }: { project: ProjectBundle; onBack: () => void }) {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const [synced, setSynced] = useState(false)
  const repositories = project.sources.filter(source => source.type === 'repo')
  const { confirm, confirmDialog } = useConfirm()
  const sessionMutation = useMutation({
    mutationFn: () => dispatch(createSlot({ project_id: project.id })).unwrap(),
    onSuccess: slot => navigate(`/chat?sid=${encodeURIComponent(slot.key)}`),
  })
  const syncMutation = useMutation({
    mutationFn: () => api.syncProjectBundle(project.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
      setSynced(true)
    },
  })
  const activationMutation = useMutation({
    mutationFn: () => project.capabilities.active
      ? api.deactivateProjectBundle(project.id)
      : api.activateProjectBundle(project.id, project.capabilities.review_key),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
    },
  })
  const removeMutation = useMutation({
    mutationFn: () => api.removeProjectBundle(project.id),
    onSuccess: async () => {
      onBack()
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
    },
  })
  const syncable = project.registrations.some(registration => registration.syncable)
  const capabilitiesEmpty = project.capabilities.agents
    + project.capabilities.skills
    + project.capabilities.mcp_servers
    + project.capabilities.repos === 0

  async function removeProject() {
    const accepted = await confirm({
      title: i18nT('pages.projectBundlesPage.remove_project_title', { name: project.name }),
      body: i18nT('pages.projectBundlesPage.remove_project_body'),
      confirmLabel: i18nT('pages.projectBundlesPage.remove_project'),
    })
    if (accepted) removeMutation.mutate()
  }

  async function toggleActivation() {
    if (project.capabilities.active) {
      activationMutation.mutate()
      return
    }
    const accepted = await confirm({
      title: i18nT('pages.projectBundlesPage.activate_project_title'),
      body: i18nT('pages.projectBundlesPage.activate_project_body'),
      confirmLabel: i18nT('pages.projectBundlesPage.trust_and_activate'),
      danger: false,
    })
    if (accepted) activationMutation.mutate()
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
              <Badge variant={project.health.status === 'healthy' ? 'ok' : 'err'}>
                {project.health.status === 'healthy' ? i18nT('pages.projectBundlesPage.healthy') : i18nT('pages.projectBundlesPage.unavailable')}
              </Badge>
            </div>
            <p className="max-w-2xl text-sm text-muted">{project.description || i18nT('pages.projectBundlesPage.no_description')}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <SendBtn className="inline-flex items-center gap-1.5" disabled={sessionMutation.isPending || project.health.status !== 'healthy'} onClick={() => sessionMutation.mutate()}>
              <MessageSquare className="lucide-inline" />
              {i18nT('pages.projectBundlesPage.new_session')}
            </SendBtn>
          </div>
        </div>
        {project.health.status !== 'healthy' && (
          <div className="mt-4 rounded-md border border-danger/40 bg-danger-subtle px-3 py-2 text-[13px] text-danger" role="alert">
            <div>{i18nT('pages.projectBundlesPage.manifest_unavailable_help')}</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {syncable && (
                <Btn disabled={syncMutation.isPending} onClick={() => { setSynced(false); syncMutation.mutate() }}>
                  <RefreshCw className="lucide-inline" />
                  {i18nT('pages.projectBundlesPage.retry_sync')}
                </Btn>
              )}
            </div>
          </div>
        )}
        {sessionMutation.error && <div className="mt-4"><RequestError error={sessionMutation.error} /></div>}
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
                <span className="shrink-0 text-[12px] text-muted">{fmtNumber(session.messages)}</span>
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
            {repositories.map(source => (
              <div className="rounded-md border border-border bg-bg-elevated px-3 py-3" key={source.id}>
                <div className="font-medium text-text">{source.id}</div>
                {typeof source.url === 'string' && <div className="mt-1 break-all font-mono text-[13px] text-muted">{source.url}</div>}
                {typeof source.default_branch === 'string' && source.default_branch && <div className="mt-1 text-[13px] text-muted">{i18nT('pages.projectBundlesPage.default_branch')}: <span className="font-mono text-text">{source.default_branch}</span></div>}
              </div>
            ))}
          </div>
        ) : <div className="text-[13px] text-muted">{i18nT('pages.projectBundlesPage.no_sources')}</div>}
      </Card>

      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="mb-1">{i18nT('pages.projectBundlesPage.included_capabilities')}</CardTitle>
            <p className="max-w-2xl text-[13px] text-muted">
              {project.capabilities.active ? i18nT('pages.projectBundlesPage.capabilities_active_help') : i18nT('pages.projectBundlesPage.capabilities_inactive_help')}
            </p>
          </div>
          <Badge variant={project.capabilities.active ? 'ok' : 'muted'}>
            {project.capabilities.active ? i18nT('pages.projectBundlesPage.active') : i18nT('pages.projectBundlesPage.inactive')}
          </Badge>
        </div>
        <div className="mt-4 space-y-3">
          {capabilitiesEmpty && (
            <div className="rounded-md border border-border bg-bg-elevated px-3 py-3 text-[13px] text-muted">
              {i18nT('pages.projectBundlesPage.populate_project_help')}
            </div>
          )}
          <CapabilityPaths count={project.capabilities.agents} icon={<Bot className="lucide-inline" />} label={i18nT('pages.projectBundlesPage.agents')} paths={project.context.agents} />
          {!!project.capabilities.agent_names?.length && (
            <div className="ml-7 flex flex-wrap gap-2">
              {project.capabilities.agent_names.map(name => (
                <Badge key={name} variant="muted">{name}</Badge>
              ))}
            </div>
          )}
          <CapabilityPaths count={project.capabilities.skills} icon={<Sparkles className="lucide-inline" />} label={i18nT('pages.projectBundlesPage.skills')} paths={project.context.skills} />
          <CapabilityPaths count={project.capabilities.mcp_servers} icon={<Server className="lucide-inline" />} label={i18nT('pages.projectBundlesPage.mcp_servers')} paths={project.context.mcp ? [project.context.mcp] : []} />
          {!!project.capabilities.mcp_server_details?.length && (
            <div className="ml-7 space-y-2">
              {project.capabilities.mcp_server_details.map(server => (
                <div className="rounded-md border border-border bg-bg-elevated px-3 py-2" key={server.name}>
                  <div className="font-medium text-text">{server.name}</div>
                  {server.command && (
                    <div className="mt-1 break-all font-mono text-[12px] text-muted">
                      {[server.command, ...(server.args ?? [])].map(argument => JSON.stringify(argument)).join(' ')}
                    </div>
                  )}
                  {server.url && <div className="mt-1 break-all font-mono text-[12px] text-muted">{server.url}</div>}
                </div>
              ))}
            </div>
          )}
          <CapabilityRepositories count={project.capabilities.repos} repositories={project.capabilities.repositories} />
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Btn disabled={activationMutation.isPending || !project.capabilities.review_key} onClick={() => { void toggleActivation() }}>
            {project.capabilities.active ? <ShieldOff className="lucide-inline" /> : <ShieldCheck className="lucide-inline" />}
            {project.capabilities.active ? i18nT('pages.projectBundlesPage.deactivate_capabilities') : i18nT('pages.projectBundlesPage.trust_and_activate')}
          </Btn>
        </div>
        {activationMutation.error && <div className="mt-3"><RequestError error={activationMutation.error} /></div>}
      </Card>

      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <CardTitle className="mb-0">{i18nT('pages.projectBundlesPage.local_copy')}</CardTitle>
          {syncable && (
            <Btn disabled={syncMutation.isPending} onClick={() => { setSynced(false); syncMutation.mutate() }}>
              <RefreshCw className="lucide-inline" />
              {i18nT('pages.projectBundlesPage.sync_project')}
            </Btn>
          )}
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
        {synced && <div className="mt-3 text-[13px] text-ok" role="status" aria-live="polite">{i18nT('pages.projectBundlesPage.project_synced')}</div>}
        {syncMutation.error && <div className="mt-3"><RequestError error={syncMutation.error} /></div>}
        <div className="mt-4 border-t border-border pt-4">
          <Btn danger disabled={removeMutation.isPending} onClick={() => { void removeProject() }}>
            <Trash2 className="lucide-inline" />
            {i18nT('pages.projectBundlesPage.remove_from_kiro_crew')}
          </Btn>
          {removeMutation.error && <div className="mt-3"><RequestError error={removeMutation.error} /></div>}
        </div>
      </Card>
      {confirmDialog}
    </div>
  )
}

function CapabilityPaths({ icon, label, paths, count }: { icon: React.ReactNode; label: string; paths: string[]; count: number }) {
  return (
    <div className="rounded-md border border-border bg-bg-elevated px-3 py-3">
      <div className="flex items-center gap-2 text-[13px] font-medium text-text">{icon}{label}<Badge variant="muted">{fmtNumber(count)}</Badge></div>
      {paths.length ? (
        <div className="mt-2 space-y-1">
          {paths.map(path => <div className="break-all font-mono text-[12px] text-muted" key={path}>{path}</div>)}
        </div>
      ) : <div className="mt-1 text-[12px] text-muted">{i18nT('pages.projectBundlesPage.none_included')}</div>}
    </div>
  )
}

function CapabilityRepositories({ count, repositories }: { count: number; repositories: ProjectBundle['capabilities']['repositories'] }) {
  return (
    <div className="rounded-md border border-border bg-bg-elevated px-3 py-3">
      <div className="flex items-center gap-2 text-[13px] font-medium text-text">
        <GitBranch className="lucide-inline" />
        {i18nT('pages.projectBundlesPage.repository_checkouts')}
        <Badge variant="muted">{fmtNumber(count)}</Badge>
      </div>
      {repositories.length ? (
        <div className="mt-2 space-y-2">
          {repositories.map(repository => (
            <div key={`${repository.source_id}:${repository.path}`}>
              <div className="font-medium text-[12px] text-text">{repository.source_id}</div>
              <div className="break-all font-mono text-[12px] text-muted">{repository.path}</div>
            </div>
          ))}
        </div>
      ) : <div className="mt-1 text-[12px] text-muted">{i18nT('pages.projectBundlesPage.none_included')}</div>}
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
              <Badge variant={project.health.status === 'healthy' ? 'ok' : 'err'}>
                {project.health.status === 'healthy' ? i18nT('pages.projectBundlesPage.healthy') : i18nT('pages.projectBundlesPage.unavailable')}
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
          <ChevronRight className="lucide-inline shrink-0 text-muted transition-transform group-hover:translate-x-0.5" />
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
        {form && !selected && <BundleForm mode={form} onClose={() => updateRoute({ view: null })} />}
        {projectsQuery.isLoading ? (
          <Card className="max-w-4xl"><ContentSkeleton rows={4} /></Card>
        ) : projectsQuery.error ? (
          <Card className="max-w-4xl"><div className="text-sm text-danger" role="alert">{i18nT('pages.projectBundlesPage.failed_to_load_projects')}</div></Card>
        ) : projects.length === 0 ? (
          <Card className="max-w-4xl">
            <EmptyState icon={<FolderKanban className="lucide-inline" />} title={i18nT('pages.projectBundlesPage.no_projects_yet')} subtitle={i18nT('pages.projectBundlesPage.empty_subtitle')} testId="project-bundles-empty" />
          </Card>
        ) : selected ? (
          <ProjectDetails
            key={selected.id}
            project={selected}
            onBack={() => updateRoute({ project: null, view: null })}
          />
        ) : (
          <ProjectList projects={projects} onOpen={id => updateRoute({ project: id, view: null })} />
        )}
      </div>
    </>
  )
}

export default function ProjectBundlesPage({ embedded = false }: { embedded?: boolean }) {
  const { search } = useLocation()
  if (new URLSearchParams(search).has('applied')) return <Navigate replace to={`/tasks${search}`} />
  return <ProjectBundlesContent embedded={embedded} />
}
