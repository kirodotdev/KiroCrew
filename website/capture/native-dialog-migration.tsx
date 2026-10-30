/**
 * Isolated capture entry for renderer dialog surfaces.
 *
 * Every surface here is the REAL component a migrated `confirm()` /
 * `prompt()` / `alert()` now goes through, opened with that call site's
 * shipped i18n strings and options. `SCENES` is the inventory: one entry per
 * distinct wording; call sites that show identical text share a scene.
 *
 *   ?scene=<id>     one dialog, notice set or live banner from SCENES
 *   ?scene=list     the scene ids, kinds and groups as JSON (for the shoot script)
 *   ?theme=dark|light
 *
 * A prompt scene with `submitEmpty` is shot after pressing its confirm button
 * on a blank field, which shows the call site's `requiredMessage`.
 *
 * An `inline-confirm` scene renders the standalone windows' confirm; with
 * `mochiTheme` it renders as the Mochi windows do: no dashboard stylesheet,
 * only the theme variables Mochi's `applyTheme` installs.
 *
 * A `page` scene renders a real page or feed in `#page-frame` with the `api`
 * calls it makes stubbed, and is shot once `ready` matches. Elements marked
 * `data-capture-crop` narrow the shot to their union.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { store } from '../src/store'
import { useConfirm, type ConfirmOptions } from '../src/components/ConfirmDialog'
import { useTextPrompt, type TextPromptOptions } from '../src/components/TextPromptDialog'
import ErrorNotice from '../src/components/ErrorNotice'
import StatusNotice from '../src/components/StatusNotice'
import NotificationBanner from '../src/components/notifications/NotificationBanner'
import { appErrorNotice } from '../src/lib/appNotice'
import { PAPYRUS_DEFAULT_COMMIT_MESSAGE } from '../src/apps/papyrus/gitWire'
import { useInlineStyledConfirm, type InlineConfirmOptions } from '../src/components/InlineStyledConfirm'
import { applyTheme as applyMochiTheme } from '../src/apps/mochi/src/shared/themes'
import { api } from '../src/api/client'
import ChannelPage from '../src/pages/ChannelPage'
import WebAppArtifactCard from '../src/components/WebAppArtifactCard'
import type { Artifact } from '../src/types'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const sceneId = params.get('scene') || 'session-close'

type Group = 'sessions' | 'leave' | 'settings' | 'apps' | 'entry'

type Scene = { where: string; group: Group } & (
  | { kind: 'confirm'; opts: () => ConfirmOptions }
  | { kind: 'inline-confirm'; mochiTheme?: boolean; opts: () => InlineConfirmOptions }
  | { kind: 'prompt'; opts: () => TextPromptOptions; submitEmpty?: boolean }
  | { kind: 'notice'; render: () => ReactNode }
  | { kind: 'banner'; messages: () => string[] }
  | { kind: 'page'; ready: string; render: () => ReactNode; wide?: boolean }
)

const t = i18nT
const KEEP = () => t('components.confirmDialog.keep_editing')
const RESOURCES = '  AWS::S3::Bucket: demo-site-assets\n  AWS::CloudFront::Distribution: E2EXAMPLE\n  AWS::Lambda::Function: demo-site-api'
const noop = () => {}

const newFile = (): TextPromptOptions => ({
  title: t('apps.papyrus.workspace.new_file_prompt'),
  confirmLabel: t('apps.papyrus.fileTree.new_file'),
  requiredMessage: t('apps.papyrus.workspace.new_file_name_required'),
})
const attachLink = (): TextPromptOptions => ({
  title: t('apps.meetings.meeting.attachPrompt'),
  confirmLabel: t('apps.meetings.pillBar.addLink'),
  requiredMessage: t('apps.meetings.meeting.attachRequired'),
})

const SCENES: Record<string, Scene> = {
  // Sessions
  'session-close': { group: 'sessions', kind: 'confirm', where: 'Sessions menu, sidebar and Cmd+W: close an idle session (setting on)', opts: () => ({ title: t('hooks.useSessionActions.close_confirm.title'), body: `${t('hooks.useSessionActions.close_confirm.reason_setting', { setting: `${t('settings.tabs.chat.label')} › ${t('pages.settings.chatPanel.confirm_before_closing_session')}` })} ${t('hooks.useSessionActions.close_confirm.actions')}`, confirmLabel: t('components.sessionActionsMenu.close_session') }) },
  'session-close-busy': { group: 'sessions', kind: 'confirm', where: 'Sessions menu, sidebar and Cmd+W: close a session with a turn running', opts: () => ({ title: t('hooks.useSessionActions.close_confirm.title'), body: `${t('hooks.useSessionActions.close_confirm.reason_turn')} ${t('hooks.useSessionActions.close_confirm.actions')}`, confirmLabel: t('components.sessionActionsMenu.close_session') }) },
  'history-delete': { group: 'sessions', kind: 'confirm', where: 'Sessions sidebar: delete a history session', opts: () => ({ title: t('pages.chatSidebar.are_you_sure_you_want_to_delete_this_history_ses'), confirmLabel: t('pages.chatSidebar.delete_history_session') }) },
  'clear-closed': { group: 'sessions', kind: 'confirm', where: 'Sessions sidebar: clear closed sessions', opts: () => ({ title: t('pages.chatSidebar.clear_closed_sessions_active_tabs_and_pinned_ses'), confirmLabel: t('pages.chatSidebar.clear') }) },
  'folder-delete': { group: 'sessions', kind: 'confirm', where: 'Sessions sidebar: delete a folder', opts: () => ({ title: t('pages.chatSidebar.delete_folder_confirm', { name: 'Release work' }), confirmLabel: t('pages.chatSidebar.delete_folder') }) },
  'column-delete': { group: 'sessions', kind: 'confirm', where: 'Sessions board: delete a column', opts: () => ({ title: t('pages.chatSidebar.delete_this_column'), confirmLabel: t('pages.chatSidebar.delete_column') }) },
  'tag-delete': { group: 'sessions', kind: 'confirm', where: 'Tag manager: delete a tag', opts: () => ({ title: t('components.tagManagerList.delete_tag_confirm_title', { name: 'ToDo' }), confirmLabel: t('components.tagManagerList.delete_tag_button') }) },
  // Leave without saving
  'leave-discard': { group: 'leave', kind: 'confirm', where: 'Artifact editor, crewmate Goals: leave with unsaved text', opts: () => ({ title: t('components.markdownPanel.discard_unsaved_changes'), confirmLabel: t('memoryV2.discard_title'), cancelLabel: KEEP() }) },
  'leave-settings': { group: 'leave', kind: 'confirm', where: 'Settings › Prompts, Agent templates, Agent config: leave with unsaved changes', opts: () => ({ title: t('pages.overview.promptsTab.discard_unsaved_changes'), confirmLabel: t('components.markdownPanel.discard_changes_button'), cancelLabel: KEEP() }) },
  'leave-memory': { group: 'leave', kind: 'confirm', where: 'Settings › Memory: leave with unsaved drafts', opts: () => ({ title: t('memoryV2.leave_discard_explanation'), confirmLabel: t('memoryV2.discard_title'), cancelLabel: KEEP() }) },
  'memory-store-switch': { group: 'leave', kind: 'confirm', where: 'Settings › Memory: switch store with unsaved drafts', opts: () => ({ title: t('memoryV2.discard_title'), body: t('memoryV2.discard_explanation'), confirmLabel: t('memoryV2.discard_title'), cancelLabel: KEEP() }) },
  'leave-crewmate-create': { group: 'leave', kind: 'confirm', where: 'New crewmate dialog: leave with a draft', opts: () => ({ title: t('pages.membersPage.create_leave_draft'), confirmLabel: t('components.markdownPanel.discard_changes_button'), cancelLabel: KEEP(), layer: 'top' }) },
  'leave-crewmate-busy': { group: 'leave', kind: 'confirm', where: 'New crewmate dialog: leave while it is being created (nothing to keep editing)', opts: () => ({ title: t('pages.membersPage.create_leave_busy'), confirmLabel: t('pages.membersPage.create_leave_busy_button'), layer: 'top' }) },
  'leave-crewmate-edit': { group: 'leave', kind: 'confirm', where: 'Crewmates: leave with unsaved crewmate edits', opts: () => ({ title: t('pages.membersPage.edit_leave_draft'), confirmLabel: t('components.markdownPanel.discard_changes_button'), cancelLabel: KEEP() }) },
  'leave-crewmate-schedule': { group: 'leave', kind: 'confirm', where: 'Crewmates: leave with a schedule being written', opts: () => ({ title: t('pages.membersPage.schedules_leave_draft'), confirmLabel: t('components.markdownPanel.discard_changes_button'), cancelLabel: KEEP() }) },
  'schedule-discard': { group: 'leave', kind: 'confirm', where: 'Crewmate profile: leave a new schedule unsaved', opts: () => ({ title: t('pages.kiroCrewAgentsPage.discard_new_schedule'), confirmLabel: t('pages.kiroCrewAgentsPage.discard_schedule_confirm'), cancelLabel: KEEP() }) },
  // Settings and Knowledge
  'knowledge-bulk-delete': { group: 'settings', kind: 'confirm', where: 'Knowledge: delete selected items', opts: () => ({ title: t('pages.knowledge.index.permanently_delete_selected_items', { count: 3 }), confirmLabel: t('pages.knowledge.index.delete') }) },
  'knowledge-item-delete': { group: 'settings', kind: 'confirm', where: 'Knowledge item view: delete the item', opts: () => ({ title: t('pages.knowledge.detailView.permanently_delete_this_item'), confirmLabel: t('pages.knowledge.detailView.delete') }) },
  'knowledge-source-remove': { group: 'settings', kind: 'confirm', where: 'Knowledge: remove a source', opts: () => ({ title: t('pages.knowledge.sourcesList.remove_this_source_and_all_its_ingested_items'), confirmLabel: t('pages.knowledge.sourcesList.remove_source') }) },
  'skill-delete': { group: 'settings', kind: 'confirm', where: 'Settings › Skills: delete a skill', opts: () => ({ title: t('pages.overview.skillsTab.delete_confirm', { name: 'code-search' }), confirmLabel: t('pages.overview.skillsTab.delete') }) },
  'skill-dismiss': { group: 'settings', kind: 'confirm', where: 'Settings › Skills: dismiss one proposal', opts: () => ({ title: t('pages.overview.skillsTab.dismiss_confirm', { name: 'release-notes' }), confirmLabel: t('pages.overview.skillsTab.dismiss') }) },
  'skill-dismiss-all': { group: 'settings', kind: 'confirm', where: 'Settings › Skills: dismiss all proposals', opts: () => ({ title: t('pages.overview.skillsTab.dismiss_all_confirm', { count: 4 }), confirmLabel: t('pages.overview.skillsTab.dismiss_all') }) },
  'agent-template-delete': { group: 'settings', kind: 'confirm', where: 'Settings › Agent templates: delete a custom agent', opts: () => ({ title: t('pages.overview.agentTemplatesTab.delete_confirm', { name: 'reviewer' }), confirmLabel: t('pages.overview.agentTemplatesTab.delete') }) },
  'prompt-delete': { group: 'settings', kind: 'confirm', where: 'Settings › Prompts: delete a prompt', opts: () => ({ title: t('pages.overview.promptsTab.delete_confirm', { name: 'weekly-digest' }), confirmLabel: t('pages.overview.promptsTab.delete') }) },
  'steering-delete': { group: 'settings', kind: 'confirm', where: 'Settings › Steering: delete a steering file', opts: () => ({ title: t('pages.overview.steeringTab.delete_confirm', { path: 'steering/style.md' }), confirmLabel: t('pages.overview.steeringTab.delete') }) },
  'portability-replace': { group: 'settings', kind: 'confirm', where: 'Settings › Portability: replace-mode import', opts: () => ({ title: t('pages.overview.portabilityTab.replace_mode_will_overwrite_existing_data_contin'), confirmLabel: t('pages.overview.portabilityTab.import') }) },
  'restart': { group: 'settings', kind: 'confirm', where: 'Restart button', opts: () => ({ title: t('components.restartButton.confirm'), confirmLabel: t('components.restartButton.apply_restart') }) },
  'theme-delete': { group: 'settings', kind: 'confirm', where: 'Theme editor: delete a custom theme', opts: () => ({ title: t('components.themeEditor.delete_this_custom_theme'), confirmLabel: t('components.themeEditor.delete_theme_confirm') }) },
  // Channels, artifacts and apps
  'channel-header': { group: 'apps', kind: 'page', wide: true, ready: '[data-capture-crop="header"]', where: 'Channel page: header row with Clear context', render: () => <ChannelHeaderScene /> },
  'channel-clear': { group: 'apps', kind: 'confirm', where: 'Channel page: clear context for all agents', opts: () => ({ title: t('pages.channelPage.this_will_reset_conversation_history_for_all_age'), confirmLabel: t('pages.channelPage.clear_context') }) },
  'channel-clear-agent': { group: 'apps', kind: 'confirm', where: 'Channel page: clear context for one agent', opts: () => ({ title: t('pages.channelPage.reset_role_s_llm_session_the_channel_s_shared_me', { role: 'reviewer' }), confirmLabel: t('pages.channelPage.clear_context') }) },
  'channel-close': { group: 'apps', kind: 'confirm', where: 'Channel page: close the channel', opts: () => ({ title: t('pages.channelPage.close_this_channel_all_agents_will_be_dismissed'), confirmLabel: t('pages.channelPage.close') }) },
  'artifact-remove': { group: 'apps', kind: 'confirm', where: 'Artifacts: remove from library', opts: () => ({ title: t('pages.artifactsPage.remove_artifact_confirm', { slug: 'q3-roadmap' }), confirmLabel: t('pages.artifactsPage.remove_from_library') }) },
  'artifact-revert': { group: 'apps', kind: 'confirm', where: 'Artifact detail: revert to a version (not destructive)', opts: () => ({ title: t('pages.artifactDetailPage.revert_confirm', { version: 3, newVersion: 6 }), confirmLabel: t('pages.artifactDetailPage.revert'), danger: false }) },
  'teardown-card': { group: 'apps', kind: 'page', wide: true, ready: '[data-capture-crop="footer"]', where: 'Web app artifact card: Tear down deployment button and its tooltip', render: () => <TeardownCardScene /> },
  'teardown': { group: 'apps', kind: 'confirm', where: 'Web app artifact: tear down a deployment', opts: () => ({ title: t('components.webAppArtifactCard.tear_down_question'), body: <p className="text-sm text-text m-0 whitespace-pre-line">{t('components.webAppArtifactCard.tear_down_confirm', { resources: RESOURCES })}</p>, confirmLabel: t('components.webAppArtifactCard.cancel_tear_down') }) },
  'auto-improvement-commit': { group: 'apps', kind: 'confirm', where: 'Auto-improvement: commit a finding', opts: () => ({ title: t('autoImprovement.commitConfirm', { branch: 'kiro/fix-retry-backoff' }), confirmLabel: t('autoImprovement.commitFinding') }) },
  'papyrus-delete-paper': { group: 'apps', kind: 'confirm', where: 'Papyrus: delete a paper', opts: () => ({ title: t('apps.papyrus.page.delete_paper_confirm', { name: 'Retrieval survey' }), confirmLabel: t('apps.papyrus.page.delete_paper_button') }) },
  'papyrus-delete-file': { group: 'apps', kind: 'confirm', where: 'Papyrus: delete a file', opts: () => ({ title: t('apps.papyrus.workspace.delete_file_confirm', { file: 'sections/intro.tex' }), confirmLabel: t('apps.papyrus.fileTree.delete_file_button') }) },
  'meeting-delete': { group: 'apps', kind: 'confirm', where: 'Meetings: delete a meeting', opts: () => ({ title: t('apps.meetings.list.deleteConfirm', { title: 'Weekly sync' }), confirmLabel: t('apps.meetings.list.deleteMeetingButton') }) },
  'design-tweak-delete': { group: 'apps', kind: 'confirm', where: 'Design Tweak: delete a request', opts: () => ({ title: t('apps.designTweak.requests.delete_confirm', { number: 12 }), confirmLabel: t('apps.designTweak.requests.delete_request') }) },
  'crew-companion-delete': { group: 'apps', kind: 'inline-confirm', where: 'Crew Companion gallery: delete an appearance pack', opts: () => ({ title: t('apps.crewCompanion.gallery.deleteConfirm', { name: 'Pastel ghosts' }), confirmLabel: t('apps.crewCompanion.gallery.delete') }) },
  'mochi-delete': { group: 'apps', kind: 'inline-confirm', mochiTheme: true, where: 'Mochi gallery: delete an appearance pack', opts: () => ({ title: t('apps.mochi.gallery.delete_confirm', { name: 'Night shift' }), confirmLabel: t('apps.mochi.gallery.delete') }) },
  // Text entry, inline notices and the live banner
  'papyrus-new-file': { group: 'entry', kind: 'prompt', where: 'Papyrus: New file', opts: newFile },
  'papyrus-new-file-required': { group: 'entry', kind: 'prompt', where: 'Papyrus: New file, submitted empty', opts: newFile, submitEmpty: true },
  'papyrus-commit': { group: 'entry', kind: 'prompt', where: 'Papyrus: commit message (blank commits with the placeholder)', opts: () => ({ title: t('apps.papyrus.workspace.commit_message_prompt'), confirmLabel: t('apps.papyrus.workspace.commit_and_push'), allowEmpty: true, placeholder: PAPYRUS_DEFAULT_COMMIT_MESSAGE }) },
  'meeting-attach-link': { group: 'entry', kind: 'prompt', where: 'Meetings: attach a link', opts: attachLink },
  'meeting-attach-link-required': { group: 'entry', kind: 'prompt', where: 'Meetings: attach a link, submitted empty', opts: attachLink, submitEmpty: true },
  'notices-research': { group: 'entry', kind: 'notice', where: 'Research Lab: export and add-to-Knowledge failures', render: () => <>
    <ErrorNotice message={t('apps.autoResearch.researchLabPage.failed_to_export_as_artifact')} askAgent variant="inline" />
    <ErrorNotice message={t('apps.autoResearch.researchLabPage.failed_to_add_to_knowledge_library')} askAgent variant="inline" />
  </> },
  'notices-agentcfg': { group: 'entry', kind: 'notice', where: 'Settings › Agent config: invalid JSON and save failure', render: () => <>
    <StatusNotice message={t('pages.overview.agentCfgTab.invalid_json')} onDismiss={noop} />
    <ErrorNotice message={t('pages.overview.agentCfgTab.save_failed')} onDismiss={noop} />
  </> },
  'notices-projects': { group: 'entry', kind: 'notice', where: 'Projects page: upload and schedule notices', render: () => <>
    <StatusNotice message={t('pages.projectsPage.yaml_files_should_be_uploaded_via_the_from_yaml')} onDismiss={noop} />
    <StatusNotice message={t('pages.projectsPage.only_yaml_yml_files_are_accepted_here_use_the_fr')} onDismiss={noop} />
    <StatusNotice message={t('pages.projectsPage.no_spec_idea_to_schedule')} onDismiss={noop} />
    <StatusNotice message={t('pages.projectsPage.scheduled_as_daily_cron_job')} tone="info" onDismiss={noop} />
  </> },
  'banner-duplicate': { group: 'entry', kind: 'banner', where: 'Live banner: duplicate-session failures', messages: () => [
    t('hooks.useSessionActions.duplicate_failed_reason', { reason: 'HTTP 500' }),
    t('hooks.useSessionActions.duplicate_guided_reason', { reason: 'The session is still starting. Try again in a moment.' }),
    t('hooks.useSessionActions.duplicate_not_found'),
    t('hooks.useSessionActions.duplicate_unreachable'),
  ] },
  'banner-reload-popout': { group: 'entry', kind: 'banner', where: 'Live banner: reload failures and blocked pop-out', messages: () => [
    t('hooks.useSessionActions.reload_failed'),
    t('hooks.useSessionActions.reload_failed_subagents'),
    t('utils.popoutController.your_browser_blocked_the_pop_out_window_allow_po'),
  ] },
}

function BannerScene({ messages }: { messages: string[] }) {
  const bellRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    // Fired last-to-first: the newest card is on top, so the deck reads in listed order.
    const timer = window.setTimeout(() => { for (const m of [...messages].reverse()) appErrorNotice(m) }, 50)
    return () => window.clearTimeout(timer)
  }, [messages])
  return <>
    <button ref={bellRef} type="button" aria-hidden="true" className="fixed top-3 right-6 w-6 h-6" tabIndex={-1} />
    <NotificationBanner bellRef={bellRef} popoverOpen={false} onOpenNote={noop} />
  </>
}

// Polls without blocking: the dialog and the notice mount on later renders.
function whenPresent<T>(find: () => T | null | undefined, then: (found: T) => void, tries = 100) {
  const found = find()
  if (found) then(found)
  else if (tries > 0) window.setTimeout(() => whenPresent(find, then, tries - 1), 50)
}

const CHANNEL = {
  id: 'ch-1', topic: 'Release 2026.10 readiness', messages: [],
  members: {
    a1: { id: 'a1', role: 'reviewer', agent_name: 'kirocrew', state: 'idle' },
    a2: { id: 'a2', role: 'release-notes', agent_name: 'kirocrew', state: 'idle' },
  },
}

/** The real Channels page with one open channel; the shot is its top band. */
function ChannelHeaderScene() {
  const frameRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    whenPresent(() => Array.from(frameRef.current?.querySelectorAll('button') ?? []).find(b => b.textContent?.trim() === t('pages.channelPage.clear_context')), button => {
      button.closest('.border-b')?.setAttribute('data-capture-crop', 'header')
      frameRef.current?.querySelector('.w-64 > .border-b')?.setAttribute('data-capture-crop', 'list')
    })
  }, [])
  return <div ref={frameRef} className="w-[860px] h-[320px] flex flex-col"><ChannelPage /></div>
}

const WEBAPP: Artifact = {
  slug: 'demo-site', name: 'Demo site', kind: 'webapp', source: 'chat', description: 'A small site deployed to AWS', tags: ['deploy'], version: 1,
  created_at: '2026-07-10T10:00:00Z', updated_at: '2026-07-10T10:00:00Z',
  webapp_metadata: {
    slug: 'demo-site', origin_session: 'abc123',
    deploy_target: { provider: 'aws', account: '123456789012', region: 'us-west-2', public_url: 'https://d111111abcdef8.cloudfront.net/demo/', profile: 'my-deploy' },
    architecture: { tier: '2', frontend: 'CloudFront -> S3 (private, OAC)', backend: 'API Gateway HTTP API -> Lambda', state: 'none', resources: [{ type: 'frontend', id: 's3://demo-site-assets/' }, { type: 'backend', id: 'demo-site-api' }] },
    lifecycle: { created_at: new Date(Date.now() - 24 * 3600e3).toISOString(), expires_at: new Date(Date.now() + 48 * 3600e3).toISOString(), persistent: false, ttl_hours: 72, status: 'live' },
    cost: { model: 'ttl-window', window_hours: 72, estimates: [{ views: 1000, usd: 0.009 }], idle_usd: 0, note: 'estimate, not the AWS bill' },
    teardown: { method: 'reaper-lambda', handle: 'demo-site', reversible: false },
  },
}

// The real card, with the button's own `title` painted beside it: headless shots do not render native tooltips.
function TeardownCardScene() {
  const frameRef = useRef<HTMLDivElement>(null)
  const [tooltip, setTooltip] = useState('')
  useEffect(() => {
    whenPresent(() => frameRef.current?.querySelector<HTMLButtonElement>(`button[aria-label="${t('components.webAppArtifactCard.cancel_tear_down')}"]`), button => {
      button.parentElement?.parentElement?.setAttribute('data-capture-crop', 'footer')
      setTooltip(button.title)
    })
  }, [])
  return <div ref={frameRef} className="w-[760px]">
    <WebAppArtifactCard artifact={WEBAPP} />
    {tooltip && <div data-capture-crop="tooltip" className="ml-auto mt-1 w-fit max-w-[420px] rounded border border-border bg-surface px-2 py-1 text-[11px] text-text shadow">{tooltip}</div>}
  </div>
}

// Before any page scene mounts: its first effects already call these.
if (SCENES[sceneId]?.kind === 'page') {
  api.channelsList = () => Promise.resolve({ channels: [CHANNEL] })
  api.channelGet = () => Promise.resolve(CHANNEL)
  api.channelPresets = () => Promise.resolve({ presets: undefined })
}
if (sceneId === 'teardown-card') {
  const realFetch = window.fetch.bind(window)
  const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } }))
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (url.includes('/app-preview')) return json({ available: false })
    if (url.includes('/api/deploy/profiles')) return json({ profiles: [{ name: 'my-deploy' }], default: 'my-deploy' })
    if (url.includes('/api/')) return json({})
    return realFetch(input, init)
  }
}

function SceneView() {
  const { confirm, confirmDialog } = useConfirm()
  const { confirm: inlineConfirm, confirmDialog: inlineConfirmDialog } = useInlineStyledConfirm()
  const { textPrompt, textPromptDialog } = useTextPrompt()
  const scene = SCENES[sceneId]
  const bannerMessages = useMemo(() => (scene?.kind === 'banner' ? scene.messages() : []), [scene])
  useEffect(() => {
    if (!scene) return
    if (scene.kind === 'confirm') void confirm(scene.opts())
    else if (scene.kind === 'inline-confirm') void inlineConfirm(scene.opts())
    else if (scene.kind === 'prompt') void textPrompt(scene.opts())
  }, [confirm, inlineConfirm, textPrompt, scene])
  if (sceneId === 'list') {
    const list = Object.entries(SCENES).map(([id, s]) => ({ id, kind: s.kind, group: s.group, where: s.where, submitEmpty: s.kind === 'prompt' && !!s.submitEmpty, ready: s.kind === 'page' ? s.ready : undefined, wide: s.kind === 'page' && !!s.wide }))
    return <pre id="scene-list">{JSON.stringify(list)}</pre>
  }
  return (
    <div className="min-h-screen bg-bg text-text p-6">
      {scene?.kind === 'notice' && <div id="notice-frame" className="w-[440px] space-y-3 p-3">{scene.render()}</div>}
      {scene?.kind === 'banner' && <BannerScene messages={bannerMessages} />}
      {scene?.kind === 'page' && <div id="page-frame" className="inline-block">{scene.render()}</div>}
      {confirmDialog}
      {inlineConfirmDialog}
      {textPromptDialog}
    </div>
  )
}

const sceneDef = SCENES[sceneId]
if (sceneDef?.kind === 'inline-confirm' && sceneDef.mochiTheme) {
  localStorage.setItem('mc-color-theme', 'kiro')
  localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
  applyMochiTheme()
} else {
  await import('../src/index.css')
  // As useTheme applies it: the light glass tints and the darkened status text
  // key on data-mode, not data-theme.
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
  document.documentElement.setAttribute('data-mode', theme === 'light' ? 'light' : 'dark')
}
initI18n('en')
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/capture']}>
        <SceneView />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
