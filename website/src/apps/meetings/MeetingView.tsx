// One meeting, live: the agent panels, the transcription controls, the task
// sidebar, and (once it has ended) the task-review gate.

import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  AlertTriangle,
  ArrowLeft,
  Languages,
  ListChecks,
  Mic,
  MicOff,
  MoreHorizontal,
  Play,
  RefreshCw,
  Square,
} from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { Badge, Btn, EmptyState, SendBtn, Skeleton } from '../../components/ui'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '../../components/ui/dropdown-menu'
import type { MeetingsConfig } from './api'
import AgentPanel from './components/AgentPanel'
import AgentPillBar from './components/AgentPillBar'
import BroadcastBar from './components/BroadcastBar'
import MeetingWorkspace from './components/MeetingWorkspace'
import TaskSidebar from './components/TaskSidebar'
import TranscriptPanel from './components/TranscriptPanel'
import TranslationSidebar from './components/TranslationSidebar'
import TaskReviewView from './TaskReviewView'
import { useMeetingSession } from './hooks/useMeetingSession'

interface Props {
  eventId: string
  fallbackTitle?: string
  config: MeetingsConfig | undefined
  onBack: () => void
  onOpenSettings: () => void
  notify: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

export default function MeetingView({
  eventId,
  fallbackTitle,
  config,
  onBack,
  onOpenSettings,
  notify,
}: Props) {
  const session = useMeetingSession({ eventId, fallbackTitle, config, notify })
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [attachMenuOpen, setAttachMenuOpen] = useState(false)

  const {
    meta,
    status,
    agents,
    enabledAgents,
    enabledIds,
    mutedAgents,
    outputs,
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
  } = session

  if (loading) return <Skeleton className="h-40 m-6" />

  if (error) {
    return (
      <EmptyState
        icon={<AlertTriangle className="lucide-inline" />}
        title={i18nT('apps.meetings.meeting.loadFailed')}
        subtitle={error.message}
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
        onBack={actions.backToMeeting}
        onClose={() => {
          actions.stop()
          onBack()
        }}
        onFile={actions.fileTask}
        onArchive={actions.archiveTask}
        onUnarchive={actions.unarchiveTask}
      />
    )
  }

  const promptForLink = () => {
    // A URL attachment is the one context source that needs no file dialog and
    // no upload endpoint, so it is what this app offers.
    const url = window.prompt(i18nT('apps.meetings.meeting.attachPrompt')) ?? ''
    const trimmed = url.trim()
    if (!trimmed) return
    let label = trimmed
    try {
      label = new URL(trimmed).hostname || trimmed
    } catch {
      /* keep the raw string as the label */
    }
    actions.addAttachment(trimmed, label)
    setAttachMenuOpen(false)
  }

  return (
    // Stacks below `lg`, the same shape MeetingWorkspace and TaskReviewView
    // already use in this app: a 340px task sidebar beside the meeting left the
    // content 49px at 390px. `min-w-0`/`min-h-0` on the content column is what
    // lets it actually shrink -- without it the row overflowed sideways instead,
    // pushing the meeting off-screen rather than narrowing it.
    <div className="flex flex-col lg:flex-row h-full overflow-hidden">
      <div className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden">
        <div className="flex-none px-4 md:px-6 py-4 border-b border-border flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <Btn onClick={onBack} aria-label={i18nT('apps.meetings.meeting.back')}>
              <ArrowLeft className="lucide-inline" />
              {i18nT('apps.meetings.meeting.back')}
            </Btn>
            <h2 className="text-lg font-semibold text-text-strong truncate">
              {meta?.title || i18nT('apps.meetings.session.untitled')}
            </h2>
            {status === 'active' && (
              <Badge variant="ok">{i18nT('apps.meetings.meeting.live')}</Badge>
            )}
            {status === 'paused' && (
              <Badge variant="warn">{i18nT('apps.meetings.meeting.paused')}</Badge>
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
            {status === 'active' && (
              <Btn onClick={actions.pause} aria-label={i18nT('apps.meetings.meeting.pause')}>
                <Mic className="lucide-inline" />
                {i18nT('apps.meetings.meeting.pause')}
              </Btn>
            )}
            {status === 'paused' && (
              <Btn onClick={actions.resume} aria-label={i18nT('apps.meetings.meeting.unpause')}>
                <MicOff className="lucide-inline" />
                {i18nT('apps.meetings.meeting.unpause')}
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
                    <DropdownMenuItem onSelect={actions.review}>
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
                    setSidebarOpen(open => !open)
                  }}
                >
                  <ListChecks size={13} className="shrink-0 text-muted" />
                  <span>
                    {i18nT('apps.meetings.meeting.toggleTasks')}
                    {tasks.length > 0 ? ` (${tasks.length})` : ''}
                  </span>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>

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
          onOpenSettings={onOpenSettings}
          onToggleAttachMenu={() => setAttachMenuOpen(open => !open)}
          onAddAttachment={promptForLink}
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
          agentPanels={(
            <div className="grid grid-cols-1 2xl:grid-cols-2 gap-3">
              {enabledAgents.map(agent => (
                <AgentPanel
                  key={agent.id}
                  agent={agent}
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
                />
              ))}
            </div>
          )}
          transcript={(
            <TranscriptPanel
              segments={transcript}
              partial={partialTranscript}
              primary={enabledAgents.length === 0}
              status={status}
              full={transcriptFull}
            />
          )}
        />

        {(status === 'active' || status === 'paused') && (
          <BroadcastBar
            onSend={actions.broadcast}
            caption={caption}
            disabled={transcriptFull}
          />
        )}
      </div>

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
    </div>
  )
}
