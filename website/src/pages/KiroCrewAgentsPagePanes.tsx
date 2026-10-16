/** Presentational pieces of the crew editor sheet: its header (face, live
 *  title, source badge, Edit avatar and Chat) and the Model, Workspace & memory
 *  and Routing pane bodies. The page owns their state and passes it in. */
import { MessageSquare, UserPen } from 'lucide-react'
import { Btn } from '../components/ui'
import {
  DialogHeader, DialogTitle,
} from '../components/ui/dialog'
import CrewAvatar, { packAvatarFrom, type CrewAvatarOverride } from '../components/CrewAvatar'
import CrewStateAvatar from '../components/CrewStateAvatar'
import CrewAvatarButton from '../components/crew/CrewAvatarButton'
import { type CrewPaneKey } from '../components/crew/crewEditorSections'
import { crewDisplayName, type KiroCrewAgent } from '../components/AgentSelector'
import { effortLabel } from '../lib/effort'
import { i18nT } from '../i18n/t'
import { uiLocation } from '../uiLocations/uiLocation'
import ErrorNotice from '../components/ErrorNotice'
import type { NavigateFunction } from 'react-router-dom'
import {
  Field,
  WorkspaceField,
  memberMemoryState,
  MemoryStoreField,
  ModelField,
  EffortField,
  TriggersField,
  SessionColorField,
} from './KiroCrewAgentsPageFields'
import { CrewSourceBadge, showsCrewSourceBadge } from './KiroCrewAgentsPageRoster'

/** Text for a failed query: the thrown message, or the value itself when the
 *  rejection was not an Error. */
export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** The editor sheet's header: the identity block and the two header actions. */
export function CrewEditorHeader({ editing, editingAgent, displayName, editAvatar, sheetBusy, openAvatarBuilder, requestChat, setError }: {
  editing: string
  editingAgent: KiroCrewAgent | undefined
  displayName: string
  editAvatar: CrewAvatarOverride | null
  sheetBusy: boolean
  openAvatarBuilder: () => void
  requestChat: () => void
  setError: (message: string) => void
}) {
  return (
    <DialogHeader className="flex-wrap sm:flex-nowrap">
      {/* The avatar is itself the entry point to the builder: the
          first-run review's top finding was that a face setting filed
          under "Triggers" has no scent — but everyone tries clicking
          the face. CrewAvatarButton makes that visible (hover scrim +
          pencil, persistent badge on touch); the "Edit avatar" button
          beside the title is the text route that needs no guessing at
          all (issue #9103). No first-run hint chip here — the text
          button IS the hint; the chip is for the Crew Members header,
          whose text route sits behind the drawer toggle.

          Two visual groups, not one row of controls: the IDENTITY block
          (face · name · source) on the left, and the ACTION group on the
          right. The face is the crew's identity that happens to be
          pressable — it is not a peer of the two labelled actions, and
          the header's action row stays at two (max-two-buttons-per-row
          counts per visual group). */}
      <div className="flex w-full min-w-0 items-center gap-3 sm:w-auto sm:flex-1" data-testid="crew-editor-identity">
        <CrewAvatarButton
          size={28}
          onEdit={openAvatarBuilder}
          // Outside the pane's <fieldset> fence, so it carries the same
          // busy gate itself: a builder opened mid-save could Apply a newer
          // draft that the completing save's close then discards.
          disabled={sheetBusy}
          data-testid="header-avatar-button"
        >
          {/* Which tier failed decides the message: CrewAvatar draws a pack's slot
              URL through the same <img>, so one banner for both told a pack
              crew its "saved picture" was broken — a picture it never had,
              and advice ("upload it again") it cannot act on. */}
          <CrewStateAvatar seed={editing} avatar={editAvatar ?? undefined} size={28} onImageError={() => setError(i18nT(packAvatarFrom(editAvatar) ? 'components.avatarBuilder.pack_load_failed' : 'components.avatarBuilder.image_load_failed'))} />
        </CrewAvatarButton>
        <DialogTitle className="flex-1 font-mono">
          {/* The draft label, live: retitling the crew is the one edit
              whose effect IS this text, so it previews before Save. */}
          {crewDisplayName({ name: editing, display_name: displayName, kiro_agent: editingAgent?.kiro_agent })}
        </DialogTitle>
        {showsCrewSourceBadge(editingAgent?.source) && <CrewSourceBadge source={editingAgent.source} />}
      </div>
      <div className="ml-auto flex items-center gap-2" data-testid="crew-editor-actions">
        <Btn onClick={openAvatarBuilder} disabled={sheetBusy} data-testid="header-edit-avatar" title={i18nT('components.avatarBuilder.edit_avatar')} aria-label={i18nT('components.avatarBuilder.edit_avatar')} {...uiLocation('agents.edit-avatar')}>
          <UserPen className="lucide-inline" aria-hidden="true" />
          {/* Both header labels fold to their icon on a phone-width
              header so the crew name keeps its room (with two labelled
              buttons the Chat label wrapped to four lines and the
              title truncated to "on…"); aria-label carries the name. */}
          <span className="hidden sm:inline">{i18nT('components.avatarBuilder.edit_avatar')}</span>
        </Btn>
        <Btn onClick={requestChat} title={i18nT('memoryV2.chat_member')} aria-label={i18nT('memoryV2.chat_member')} {...uiLocation('agents.chat')}>
          <MessageSquare className="lucide-inline" aria-hidden="true" />
          <span className="hidden sm:inline">{i18nT('memoryV2.chat_member')}</span>
        </Btn>
      </div>
    </DialogHeader>
  )
}

/** The Model pane: the model and effort pins and what they resolve to. */
export function CrewModelPane({ modelOptions, pinModelsQuery, pinHarness, servedAs, editModel, setEditModel, editEffort, setEditEffort, effortModel, effortCapable, effortLevels, resolved, resolvedError }: {
  modelOptions: string[]
  /** The pin harness's model list: it keeps no last-good copy, so a failure leaves only Inherited. */
  pinModelsQuery: { isError: boolean; isDegraded: boolean; isFetching: boolean; refetch: () => unknown }
  /** That harness's display name. */
  pinHarness: string
  /** `resolved-model`'s `pin_served_as` for the stored pin, while it is the one shown. */
  servedAs?: string
  editModel: string
  setEditModel: (v: string) => void
  editEffort: string
  setEditEffort: (v: string) => void
  effortModel: string
  effortCapable: boolean
  /** The levels the pin harness takes, when it reported its own. */
  effortLevels?: readonly string[]
  resolved: { model?: string; pinned?: boolean; reasoning_effort?: string; effort_pinned?: boolean; effort_supported?: boolean | null; models_backend?: string } | undefined
  resolvedError: Error | null
}) {
  return (
    <>
      <ModelField options={modelOptions} value={editModel} onChange={setEditModel} servedAs={servedAs} harness={pinHarness} />
      {/* No hand-off: the crew sheet's unsaved pane edits (dirtyPanes). Another
          harness's list keeps no last-good copy, so a failure leaves only Inherited. */}
      {(pinModelsQuery.isError || pinModelsQuery.isDegraded) && (
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <ErrorNotice
            className="min-w-[12rem] flex-1"
            message={resolved?.models_backend === undefined
              ? i18nT('pages.chatSidebar.model_list_failed')
              : i18nT('pages.kiroCrewAgentsPage.harness_models_failed', { harness: pinHarness })}
            testId="crew-editor-models-degraded"
          />
          <Btn type="button" className="shrink-0" onClick={() => pinModelsQuery.refetch()} disabled={pinModelsQuery.isFetching}>
            {i18nT('pages.chatSidebar.retry')}
          </Btn>
        </div>
      )}
      {/* Offered when the model the crew will actually run on
          accepts effort — OR when a pin is already stored on a
          model that does not, so the only way to clear a
          stranded pin is not to first switch the model back. */}
      {(effortCapable || !!editEffort) && (
        <EffortField value={editEffort} onChange={setEditEffort} levels={effortLevels} />
      )}
      {!effortCapable && !!editEffort && (
        <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
          {/* Two different reasons a stored pin cannot apply, and
              they need different sentences: naming a model only
              works when there IS one. With nothing resolved,
              substituting the "Inherited" label would read as
              "Inherited does not take a reasoning effort", which
              names no model and states nothing true. */}
          {resolved?.effort_supported === false
            ? i18nT('pages.kiroCrewAgentsPage.effort_ignored_by_this_backend', { harness: pinHarness })
            : effortModel
            ? i18nT('pages.kiroCrewAgentsPage.effort_ignored_on_this_model', { model: effortModel })
            : i18nT('pages.kiroCrewAgentsPage.effort_pin_needs_a_model')}
        </div>
      )}
      {/* No hand-off: the crew sheet's unsaved pane edits
          (dirtyPanes). Without this a failed resolve just left the
          readout below absent, as if the crew had no model. */}
      <ErrorNotice
        message={resolvedError ? errorText(resolvedError) : null}
        testId="crew-resolved-model-error"
      />
      {resolved && (
        <div className="flex flex-col gap-1 rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
          <div>
            <span className="text-text">
              {i18nT('pages.kiroCrewAgentsPage.resolves_to', { model: resolved.model || i18nT('pages.kiroCrewAgentsPage.inherited') })}
            </span>
            {' — '}
            {resolved.pinned
              ? i18nT('pages.kiroCrewAgentsPage.pinned_on_this_crew')
              : resolved.model
                ? i18nT('pages.kiroCrewAgentsPage.inherited_from_the_agent_template')
                : i18nT('pages.kiroCrewAgentsPage.no_pin_anywhere_the_backend_chooses')}
          </div>
          {/* The effort half of the same readout. It answers
              "what will this crew think at" in every case,
              including the one where no level can apply — an
              absent control with no line about it is what makes
              the setting look missing rather than unavailable.
              Suppressed only for the stranded pin, where the
              warning above already says it and says what to do. */}
          {(effortCapable || !editEffort) && (
            <div>
              {effortCapable ? (
                <>
                  <span className="text-text">
                    {i18nT('pages.kiroCrewAgentsPage.effort_resolves_to', {
                      effort: resolved.reasoning_effort
                        ? effortLabel(resolved.reasoning_effort)
                        : i18nT('lib.effort.default'),
                    })}
                  </span>
                  {' — '}
                  {resolved.effort_pinned
                    ? i18nT('pages.kiroCrewAgentsPage.pinned_on_this_crew')
                    : resolved.reasoning_effort
                      ? i18nT('pages.kiroCrewAgentsPage.effort_inherited_from_the_global_default')
                      : i18nT('pages.kiroCrewAgentsPage.no_effort_pin_the_model_decides')}
                </>
              ) : effortModel ? (
                i18nT('pages.kiroCrewAgentsPage.effort_unavailable_on_this_model', { model: effortModel })
              ) : (
                i18nT('pages.kiroCrewAgentsPage.effort_needs_a_model')
              )}
            </div>
          )}
        </div>
      )}
    </>
  )
}

/** The Workspace & memory pane. */
export function CrewPlacePane({ editing, workspaceOptions, workspace, setWorkspace, setWsModalOpen, memoryStore, kirocrewCfg, sheetBusy, dirtyPanes, schedDraft, navigate, collidingCrews }: {
  editing: string
  workspaceOptions: string[]
  workspace: string
  setWorkspace: (v: string) => void
  setWsModalOpen: (open: boolean) => void
  memoryStore: string
  kirocrewCfg: { memory_stores?: Record<string, { memory_version?: number; owner_member?: string }> } | undefined
  sheetBusy: boolean
  dirtyPanes: Set<CrewPaneKey>
  schedDraft: boolean
  navigate: NavigateFunction
  collidingCrews: string[]
}) {
  return (
    <>
      <WorkspaceField
        options={workspaceOptions}
        value={workspace}
        onChange={setWorkspace}
        onNewWorkspace={() => setWsModalOpen(true)}
        subject="agent"
      />
      <MemoryStoreField
        value={memoryStore}
        member={editing}
        memoryState={memberMemoryState(editing, memoryStore, kirocrewCfg?.memory_stores)}
        busy={sheetBusy || !kirocrewCfg}
        manageDisabled={dirtyPanes.size > 0 || schedDraft}
        onManage={() => navigate(`/settings/overview?view=memory&store=${encodeURIComponent(editing === 'default' ? 'default' : memoryStore)}`)}
      />
      {/* The default assistant's shared-workspace warning does not
          describe another member's memory ownership. */}
      {editing === 'default' && collidingCrews.length > 0 && (
        <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
          {i18nT('pages.kiroCrewAgentsPage.also_used_by_these_crews', { crews: collidingCrews.join(', ') })}
        </div>
      )}
    </>
  )
}

/** The Routing pane: routing keywords, session color and the avatar entry. */
export function CrewRoutingPane({ editing, triggers, setTriggers, sessionColor, setSessionColor, editAvatar, openAvatarBuilder }: {
  editing: string
  triggers: string
  setTriggers: (v: string) => void
  sessionColor: string
  setSessionColor: (v: string) => void
  editAvatar: CrewAvatarOverride | null
  openAvatarBuilder: () => void
}) {
  return (
    <>
      <TriggersField value={triggers} onChange={setTriggers} subject="agent" />
      <SessionColorField value={sessionColor} onChange={setSessionColor} subject="agent" />
      <Field
        label={i18nT('components.avatarBuilder.field_label')}
        hint={i18nT('components.avatarBuilder.field_hint')}
      >
        <div className="flex items-center gap-2.5">
          <CrewAvatarButton size={36} onEdit={openAvatarBuilder} data-testid="field-avatar-button">
            <CrewAvatar seed={editing || ''} avatar={editAvatar ?? undefined} size={36} />
          </CrewAvatarButton>
          <Btn onClick={openAvatarBuilder} data-testid="open-avatar-builder">
            <UserPen className="lucide-inline" aria-hidden="true" />
            {i18nT('components.avatarBuilder.edit_avatar')}
          </Btn>
          {editAvatar && (
            <span className="text-[11px] text-muted">
              {i18nT('components.avatarBuilder.customized_note')}
            </span>
          )}
        </div>
      </Field>
    </>
  )
}
