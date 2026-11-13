// One meeting, live: the agent panels, the transcription controls, the task
// sidebar, and (once it has ended) the task-review gate.

import { useEffect, useLayoutEffect, useRef, useState, type MutableRefObject } from 'react'
import { useNavigate } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import {
  AlertTriangle,
  ArrowLeft,
  Languages,
  ListChecks,
  MoreHorizontal,
  NotebookPen,
  Pause as PauseIcon,
  Play,
  RefreshCw,
  Square,
} from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { settingsPath } from '../../components/settingsPath'
import { useTextPrompt } from '../../components/TextPromptDialog'
import { Badge, Btn, SendBtn, Skeleton } from '../../components/ui'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '../../components/ui/dropdown-menu'
import type { MeetingsConfig } from './api'
import AgentPanel from './components/AgentPanel'
import AgentPanelSlot from './components/AgentPanelSlot'
import AgentPillBar from './components/AgentPillBar'
import BroadcastBar from './components/BroadcastBar'
import ExportMenuItems from './components/ExportMenuItems'
import MeetingTitle from './components/MeetingTitle'
import MeetingWorkspace from './components/MeetingWorkspace'
import NoteSidebar from './components/NoteSidebar'
import TaskSidebar from './components/TaskSidebar'
import TranscriptPanel from './components/TranscriptPanel'
import TranslationSidebar from './components/TranslationSidebar'
import TaskReviewView from './TaskReviewView'
import { useExpandedAgent } from './hooks/useExpandedAgent'
import { useMeetingSession } from './hooks/useMeetingSession'

interface Props {
  eventId: string
  fallbackTitle?: string
  config: MeetingsConfig | undefined
  onBack: () => void
  onOpenSettings: () => void
  /** Open another meeting (the one a refused start names). */
  onOpenMeeting?: (eventId: string, title: string) => void
  notify: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

interface AttachLinkPromptProps {
  promptRef: MutableRefObject<(() => Promise<void>) | null>
  onAddAttachment: (url: string, label: string) => void
  onCloseMenu: () => void
}

/** Leaving the keyed meeting or live view unmounts this owner, cancelling its prompt. */
function AttachLinkPrompt({ promptRef, onAddAttachment, onCloseMenu }: AttachLinkPromptProps) {
  const { textPrompt, textPromptDialog } = useTextPrompt()
  const active = useRef(false)
  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  useEffect(() => {
    // A link needs neither a file dialog nor an upload endpoint, so it is the one attachment this app offers.
    const promptForLink = async () => {
      const url = await textPrompt({
        title: i18nT('apps.meetings.pillBar.addLink'),
        body: i18nT('apps.meetings.meeting.attachPrompt'),
        confirmLabel: i18nT('apps.meetings.pillBar.addLink'),
        requiredMessage: i18nT('apps.meetings.meeting.attachRequired'),
      })
      if (!active.current) return
      if (url !== null) {
        const trimmed = url.trim()
        let label = trimmed
        try {
          label = new URL(trimmed).hostname || trimmed
        } catch {
          /* Non-URL text is still attached and labels itself. */
        }
        onAddAttachment(trimmed, label)
      }
      // Cancel closes the menu too: focus returns to the trigger, outside the menu's Escape handler.
      onCloseMenu()
    }
    promptRef.current = promptForLink
    return () => {
      promptRef.current = null
    }
  }, [onAddAttachment, onCloseMenu, promptRef, textPrompt])

  return <>{textPromptDialog}</>
}

export default function MeetingView({
  eventId,
  fallbackTitle,
  config,
  onBack,
  onOpenSettings,
  onOpenMeeting,
  notify,
}: Props) {
  const session = useMeetingSession({ eventId, fallbackTitle, config, notify })
  const navigate = useNavigate()
  const promptForLinkRef = useRef<(() => Promise<void>) | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  // Agent ids whose AgentPanel holds an unsaved minutes draft. Those drafts are
  // panel-local and unmount with this view, which `leave` cannot save.
  const dirtyMinutes = useRef(new Set<string>())
  // The inputs marked `data-unsent` are uncontrolled: their unsent text lives
  // only in the DOM under this root.
  const rootRef = useRef<HTMLDivElement>(null)
  const { confirm, confirmDialog } = useConfirm()
  // Asks before an exit drops what this view holds and cannot keep: a minutes
  // draft, or text typed and never sent. Blank text is what Send refuses, so it
  // does not count. Checked only after the note save: these fields stay editable
  // while the note saves, so a check made before it would miss text typed in the
  // meantime. The dialog is modal, so nothing new is typed between its answer
  // and the navigation. Both wordings name the exit and what is lost: confirming
  // leaves the meeting, not just the editor.
  const unsavedMayBeDiscarded = async () => {
    // `input` too: agent output renders under this root, and its sanitized
    // markdown may carry the same attribute on an element with no value.
    const inputs = rootRef.current?.querySelectorAll<HTMLInputElement>('input[data-unsent]')
    const unsent = Array.from(inputs ?? []).some(input => input.value.trim() !== '')
    const minutes = dirtyMinutes.current.size > 0
    if (!minutes && !unsent) return true
    return confirm(
      unsent
        ? {
            title: i18nT('apps.meetings.meeting.leaveUnsent'),
            body: i18nT(
              minutes
                ? 'apps.meetings.meeting.leaveUnsentWithEditsHint'
                : 'apps.meetings.meeting.leaveUnsentHint',
            ),
            confirmLabel: i18nT('apps.meetings.meeting.leaveUnsentConfirm'),
          }
        : {
            title: i18nT('apps.meetings.meeting.leaveWithEdits'),
            body: i18nT('apps.meetings.meeting.leaveWithEditsHint'),
            confirmLabel: i18nT('apps.meetings.meeting.leaveUnsentConfirm'),
          },
    )
  }

  // Every way out of this view goes through here. The note draft lives in the
  // session hook and unmounts with this view, so the exit first waits for its save
  // and navigates only once the note is on disk; a refusal keeps the user here, where
  // the panel is showing that refusal, instead of navigating away from the only copy.
  // Then it asks before dropping anything else unsaved (`unsavedMayBeDiscarded`).
  // Browser-level navigation is not intercepted — see `useNoteDraft` for what covers it.
  // `to` is where the exit goes: the meeting list by default, settings from the
  // agent bar or the transcription notice, or the meeting a refused start names;
  // each unmounts this view.
  const leave = async (to: () => void = onBack) => {
    if (!(await session.leaveNote())) {
      // A refusal opens the note panel; the task sidebar closes so only one shows.
      setSidebarOpen(false)
      return
    }
    if (await unsavedMayBeDiscarded()) to()
  }
  const openMeeting = (id: string, title: string) => leave(() => onOpenMeeting?.(id, title))
  const [attachMenuOpen, setAttachMenuOpen] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

  const {
    meta,
    status,
    agents,
    enabledAgents,
    enabledIds,
    mutedAgents,
    outputs,
    startError,
    startErrorMessage,
    startBlockingMeeting,
    transcriptionErrorMessage,
    transcriptionErrorCode,
    dismissStartError,
    dismissTranscriptionError,
    outputEdits,
    editingOutput,
    tasks,
    transcript,
    partialTranscript,
    transcriptFull,
    caption,
    chatViewAgents,
    selectedPreset,
    loading,
    error,
    agentsPaused,
    syncing,
    actions,
    pending,
    translation,
    note,
  } = session

  // The one panel enlarged to fill the workspace, or null (compact, the default).
  // Disabling that agent clears the choice; see the hook.
  const { expandedId, toggle: toggleExpanded } = useExpandedAgent(
    enabledAgents.map(agent => agent.id),
  )

  if (loading) return <Skeleton className="h-40 m-6" />

  if (error) {
    // A failed read is an error, not an empty state. Nothing else of the
    // meeting rendered (the notes draft lives further down), so hand it off.
    return (
      <ErrorNotice
        title={i18nT('apps.meetings.meeting.loadFailed')}
        message={error.message}
        askAgent
        className="m-6"
      />
    )
  }

  if (status === 'reviewing') {
    return (
      <TaskReviewView
        tasks={tasks}
        transcript={transcript}
        partialTranscript={partialTranscript}
        transcriptFull={transcriptFull}
        provider={config?.task_provider ?? ''}
        filing={pending.filing}
        filingIds={pending.filingIds}
        closing={pending.stopping}
        // The stop route has no 409, so a failed End is always the plain stop failure.
        closeError={session.stopError ? i18nT('apps.meetings.session.stopFailed') : null}
        leaving={pending.settingStatus !== null}
        onBack={actions.backToMeeting}
        // Leave the meeting once the stop has landed, through the same note
        // check as Back; the control reads as pending until then, and a stop
        // that failed keeps the view, with its notice, so End can be tried again.
        onClose={() => {
          void actions.stop().then(landed => { if (landed) void leave() })
        }}
        onFile={actions.fileTask}
        onArchive={actions.archiveTask}
        onUnarchive={actions.unarchiveTask}
      />
    )
  }

  return (
    // Stacks below `lg`, the same shape MeetingWorkspace and TaskReviewView
    // already use in this app: a 340px task sidebar beside the meeting left the
    // content 49px at 390px. `min-w-0`/`min-h-0` on the content column is what
    // lets it actually shrink -- without it the row overflowed sideways instead,
    // pushing the meeting off-screen rather than narrowing it.
    <div ref={rootRef} className="flex flex-col lg:flex-row h-full overflow-hidden">
      <div className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden">
        <div className="flex-none px-4 md:px-6 py-4 border-b border-border flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <Btn onClick={() => { void leave() }} aria-label={i18nT('apps.meetings.meeting.back')}>
              <ArrowLeft className="lucide-inline" />
              {i18nT('apps.meetings.meeting.back')}
            </Btn>
            <MeetingTitle title={meta?.title ?? ''} onRename={actions.rename} />
            {status === 'active'
              && pending.settingStatus !== 'paused'
              && pending.settingStatus !== 'reviewing' && (
              <Badge variant="ok">{i18nT('apps.meetings.meeting.live')}</Badge>
            )}
            {(status === 'active' || status === 'paused')
              && pending.settingStatus === 'reviewing' && (
              <Badge variant="warn">{i18nT('apps.meetings.meeting.endingReview')}</Badge>
            )}
            {status === 'active' && pending.settingStatus === 'paused' && (
              // Transitional badge: the polled status still reads active while
              // the pause request is in flight, but the server may already
              // have closed ingress — "Live" would contradict the disabled
              // controls next to it.
              <Badge variant="warn">{i18nT('apps.meetings.meeting.pausing')}</Badge>
            )}
            {status === 'paused'
              && pending.settingStatus !== 'active'
              && pending.settingStatus !== 'reviewing' && (
              <Badge variant="warn">{i18nT('apps.meetings.meeting.paused')}</Badge>
            )}
            {status === 'paused' && pending.settingStatus === 'active' && (
              <Badge variant="warn">{i18nT('apps.meetings.meeting.resuming')}</Badge>
            )}
            {status === 'ended' && (
              <Badge variant="muted">{i18nT('apps.meetings.meeting.ended')}</Badge>
            )}
          </div>

          <div className="flex items-center gap-2 flex-wrap justify-end">
            {(status === 'idle' || status === 'ended') && (
              <SendBtn
                onClick={actions.start}
                disabled={pending.starting}
                aria-label={
                  status === 'ended'
                    ? i18nT('apps.meetings.meeting.restart')
                    : i18nT('apps.meetings.meeting.start')
                }
              >
                <Play className="lucide-inline" />
                {status === 'ended'
                  ? i18nT('apps.meetings.meeting.restart')
                  : i18nT('apps.meetings.meeting.start')}
              </SendBtn>
            )}
            {status === 'active' && pending.settingStatus !== 'reviewing' && (
              <Btn
                onClick={actions.pause}
                disabled={pending.settingStatus !== null}
                aria-label={
                  pending.settingStatus === 'paused'
                    ? i18nT('apps.meetings.meeting.pausing')
                    : i18nT('apps.meetings.meeting.pause')
                }
              >
                <PauseIcon className="lucide-inline" />
                {pending.settingStatus === 'paused'
                  ? i18nT('apps.meetings.meeting.pausing')
                  : i18nT('apps.meetings.meeting.pause')}
              </Btn>
            )}
            {status === 'paused' && (
              <Btn
                onClick={actions.resume}
                disabled={pending.settingStatus !== null}
                aria-label={
                  pending.settingStatus === 'active'
                    ? i18nT('apps.meetings.meeting.resuming')
                    : i18nT('apps.meetings.meeting.unpause')
                }
              >
                <Play className="lucide-inline" />
                {pending.settingStatus === 'active'
                  ? i18nT('apps.meetings.meeting.resuming')
                  : i18nT('apps.meetings.meeting.unpause')}
              </Btn>
            )}
            {/* Everything past the one primary status action lives in an overflow
                menu: five sibling buttons breached the two-per-row cap and wrapped
                under width pressure. The trigger counts as one control, and the
                menu items keep full text labels the icon-only buttons never had.
                Following components/CronRowActions.tsx and issue-radar's
                DetailOverflowMenu rather than inventing a second overflow shape —
                including moving a significant action (End and review, like the
                issue pane's Close) into the menu, where its full label survives. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Btn
                  className="!px-1.5"
                  aria-label={i18nT('apps.meetings.meeting.moreActions')}
                  title={i18nT('apps.meetings.meeting.moreActions')}
                >
                  <MoreHorizontal size={14} />
                </Btn>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[200px]">
                {(status === 'active' || status === 'paused') && (
                  <>
                    <DropdownMenuItem
                      disabled={pending.settingStatus !== null}
                      onSelect={actions.review}
                    >
                      <Square size={13} className="shrink-0 text-danger" />
                      <span>{i18nT('apps.meetings.meeting.endAndReview')}</span>
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                  </>
                )}
                <DropdownMenuItem disabled={syncing} onSelect={session.refresh}>
                  <RefreshCw
                    size={13}
                    className={`shrink-0 text-muted ${syncing ? 'animate-spin' : ''}`}
                  />
                  <span>{i18nT('apps.meetings.meeting.refresh')}</span>
                </DropdownMenuItem>
                <DropdownMenuItem
                  onSelect={() => {
                    // One side panel at a time (see the translation item below).
                    setSidebarOpen(false)
                    session.setTranslationOpen(false)
                    session.setNoteOpen(open => !open)
                  }}
                >
                  <NotebookPen size={13} className="shrink-0 text-muted" />
                  <span>{i18nT('apps.meetings.meeting.toggleNote')}</span>
                </DropdownMenuItem>
                {/* Offered only when a target language is configured: with translation
                    off (the default) the item would open a panel that can never fill.
                    Settings is where it gets turned on. */}
                {translation.language && (
                  <DropdownMenuItem
                    onSelect={() => {
                      // One side panel at a time: stacked below `lg` the two
                      // panels' 260px height floors together exceed a short
                      // viewport and squeeze the transcript out entirely.
                      setSidebarOpen(false)
                      session.setNoteOpen(false)
                      session.setTranslationOpen(open => !open)
                    }}
                  >
                    <Languages size={13} className="shrink-0 text-muted" />
                    <span>{i18nT('apps.meetings.meeting.toggleTranslation')}</span>
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  onSelect={() => {
                    session.setTranslationOpen(false)
                    session.setNoteOpen(false)
                    setSidebarOpen(open => !open)
                  }}
                >
                  <ListChecks size={13} className="shrink-0 text-muted" />
                  <span>
                    {i18nT('apps.meetings.meeting.toggleTasks')}
                    {tasks.length > 0 ? ` (${tasks.length})` : ''}
                  </span>
                </DropdownMenuItem>
                <ExportMenuItems
                  agents={enabledAgents}
                  outputs={outputs}
                  title={meta?.title ?? ''}
                  date={meta?.started_at || meta?.created_at}
                  onError={setExportError}
                />
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>

        {/* The one surface for a refused start or a transcription error while
             the meeting is open (no toast as well). Streaming-off links to its
             setting; the start notice's copy names the button to use.
             No hand-off: the minutes drafts in the AgentPanel below are unsaved
             local state, and capture is live. Navigating to chat would unmount
             this view, discard those drafts and stop the microphone. */}
        {transcriptionErrorMessage && (
          <ErrorNotice
            message={transcriptionErrorMessage}
            onDismiss={dismissTranscriptionError}
            className="mx-4 mt-2 animate-rise"
            footer={
              transcriptionErrorCode === 'stt_streaming_disabled' ? (
                <button
                  type="button"
                  className="underline"
                  onClick={() => void leave(() => navigate(settingsPath({ tab: 'voice' })))}
                >
                  {i18nT('apps.meetings.session.openVoiceSettings')}
                </button>
              ) : null
            }
          />
        )}
        {/* No hand-off: the minutes drafts in the AgentPanel below are unsaved. */}
        <ErrorNotice
          message={exportError}
          onDismiss={() => setExportError(null)}
          className="mx-4 mt-2 animate-rise"
        />
        {/* No hand-off to chat: the minutes drafts and any unsent chat,
             broadcast or quick-add text below are unsaved. The one action
             opens the meeting that holds the slot through the same `leave`
             as the back button. That meeting opens on its review screen when
             it is in review, because MeetingView renders TaskReviewView for
             that status. */}
        {confirmDialog}
        {startError && startErrorMessage && (
          <ErrorNotice
            message={startErrorMessage}
            onDismiss={dismissStartError}
            className="mx-4 mt-2 animate-rise"
            footer={
              startBlockingMeeting && onOpenMeeting ? (
                <button
                  type="button"
                  className="underline"
                  onClick={() =>
                    void openMeeting(startBlockingMeeting.id, startBlockingMeeting.title)
                  }
                >
                  {startBlockingMeeting.reviewing
                    ? i18nT('apps.meetings.session.openBlockingReview')
                    : i18nT('apps.meetings.session.openBlockingMeeting')}
                </button>
              ) : null
            }
          />
        )}
        <AgentPillBar
          agents={agents}
          enabledIds={enabledIds}
          mutedAgents={mutedAgents}
          presets={config?.presets ?? {}}
          defaultPreset={config?.default_preset ?? ''}
          selectedPreset={selectedPreset}
          status={status}
          attachments={meta?.attachments ?? []}
          attachMenuOpen={attachMenuOpen}
          onPresetChange={session.setSelectedPreset}
          onToggleAgent={actions.toggleAgent}
          onOpenSettings={() => { void leave(onOpenSettings) }}
          onToggleAttachMenu={() => setAttachMenuOpen(open => !open)}
          // AttachLinkPrompt owns cancellation when the live meeting leaves.
          onAddAttachment={() => promptForLinkRef.current?.()}
          onRemoveAttachment={actions.removeAttachment}
        />

        <AnimatePresence>
          {agentsPaused && (
            <motion.div
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              className="flex-none mx-6 mt-3 px-3 py-2 rounded-lg bg-danger/10 border border-danger/20 flex items-center justify-between gap-3"
            >
              <span className="text-[13px] text-danger font-medium inline-flex items-center gap-1.5">
                <AlertTriangle className="lucide-inline" />
                {i18nT('apps.meetings.meeting.agentsPaused')}
              </span>
              <Btn onClick={actions.resetAgents}>
                {i18nT('apps.meetings.meeting.retryAgents')}
              </Btn>
            </motion.div>
          )}
        </AnimatePresence>

        <MeetingWorkspace
          hasAgentPanels={enabledAgents.length > 0}
          expanded={expandedId !== null}
          agentPanels={(
            <div
              className={
                expandedId !== null
                  ? 'flex flex-col h-full'
                  : 'grid grid-cols-1 2xl:grid-cols-2 gap-3'
              }
            >
              {enabledAgents.map(agent => (
                // The other panels stay MOUNTED, only hidden: one of them can
                // hold an unsaved minutes draft, which unmounting would drop.
                <AgentPanelSlot
                  key={agent.id}
                  hidden={expandedId !== null && expandedId !== agent.id}
                  expanded={expandedId === agent.id}
                >
                  <AgentPanel
                    agent={agent}
                    expanded={expandedId === agent.id}
                    onToggleExpanded={() => toggleExpanded(agent.id)}
                    output={outputs[agent.id] ?? ''}
                    listening={!mutedAgents.includes(agent.id)}
                    chatView={chatViewAgents.includes(agent.id)}
                    edit={outputEdits[agent.id]}
                    editSaving={editingOutput}
                    onToggleListening={() =>
                      actions.mute(agent.id, !mutedAgents.includes(agent.id))
                    }
                    onToggleChatView={() => session.toggleChatView(agent.id)}
                    onSendMessage={text => actions.messageAgent(agent.id, text)}
                    // Passed only for a markdown agent, and the ABSENCE is what disables
                    // the affordance in the panel. The server enforces the same rule
                    // (`_editable_agent`); this keeps the button from appearing where
                    // pressing it could only produce a 409.
                    onSaveOutput={
                      agent.widget_type === 'markdown' || agent.widget_type == null
                        ? content => session.saveOutput(agent.id, content)
                        : undefined
                    }
                    onRevertOutput={() => session.revertOutput(agent.id)}
                    onDirtyChange={dirty => {
                      if (dirty) dirtyMinutes.current.add(agent.id)
                      else dirtyMinutes.current.delete(agent.id)
                    }}
                  />
                </AgentPanelSlot>
              ))}
            </div>
          )}
          transcript={(
            <TranscriptPanel
              segments={transcript}
              partial={partialTranscript}
              primary={enabledAgents.length === 0}
              status={status}
              resuming={pending.settingStatus === 'active'}
              full={transcriptFull}
            />
          )}
        />

        {(status === 'active' || status === 'paused') && (
          <BroadcastBar
            onSend={actions.broadcast}
            caption={caption}
            // Gated on the pending window as well: during a pending pause the
            // polled status still reads active while the server has already
            // closed ingress, and a send landing there loses the typed line
            // to a 409.
            disabled={transcriptFull || status !== 'active' || pending.settingStatus !== null}
            placeholder={
              pending.settingStatus === 'paused'
                ? i18nT('apps.meetings.broadcastBar.pausingPlaceholder')
                : pending.settingStatus === 'active'
                  ? i18nT('apps.meetings.broadcastBar.resumingPlaceholder')
                  : pending.settingStatus === 'reviewing'
                    ? i18nT('apps.meetings.meeting.endingReview')
                    : status === 'paused'
                      ? i18nT('apps.meetings.broadcastBar.pausedPlaceholder')
                      : undefined
            }
          />
        )}
      </div>

      {note.open && (
        <NoteSidebar
          draft={note.draft}
          dirty={note.dirty}
          loaded={note.loaded}
          updatedAt={note.updatedAt}
          path={note.path}
          saving={note.saving}
          saveFailed={note.saveFailed}
          loadFailed={note.loadFailed}
          uploading={note.uploading}
          uploadError={note.uploadError}
          onChange={session.changeNote}
          onFlush={session.flushNote}
          onPasteImage={session.pasteNoteImage}
          onClose={() => session.setNoteOpen(false)}
        />
      )}

      {translation.open && translation.language && (
        <TranslationSidebar
          lines={translation.lines}
          languageLabel={translation.languageLabel}
          pending={translation.pending}
          dropped={translation.dropped}
          loading={translation.loading}
          onClose={() => session.setTranslationOpen(false)}
        />
      )}

      {sidebarOpen && (
        <TaskSidebar
          tasks={tasks}
          onClose={() => setSidebarOpen(false)}
          onAdd={actions.addTask}
          onUpdate={actions.updateTask}
          onDelete={actions.deleteTask}
        />
      )}
      <AttachLinkPrompt
        key={eventId}
        promptRef={promptForLinkRef}
        onAddAttachment={actions.addAttachment}
        onCloseMenu={() => setAttachMenuOpen(false)}
      />
    </div>
  )
}
