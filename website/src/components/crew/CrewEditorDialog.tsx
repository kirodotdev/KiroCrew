/**
 * CrewEditorDialog — the crew editor's EDIT-mode modal. Per CREW-18688 it is
 * mounted in place on the Crewmates page (`MembersPage`) instead of navigating
 * to `/capabilities?tab=crews&crew=<name>`. Its ONLY consumer today is
 * MembersPage; the Crews page (`KiroCrewAgentsPage`) still renders its own
 * inline edit sheet — folding that page onto this shared dialog (so "one
 * editor" is literally true in code) is the declared follow-up.
 *
 * This component is presentation + composition only. Every stateful decision —
 * the stale-write epoch guard, the serialized template-switch chain, avatar
 * staging, the discard question, dirty-pane tracking — lives in `useCrewEditor`,
 * the shared state machine this dialog and the crew manager's sheet are meant to
 * converge on. The markup here is the edit branch lifted from
 * `KiroCrewAgentsPage`'s sheet; the create form stays on that page.
 *
 * Field components (Field, WorkspaceModal, the per-binding fields) are imported
 * from `KiroCrewAgentsPage` — a component-usage import the bundler resolves
 * because the bindings are read only inside render, the same shape as the
 * existing MembersPage -> NewCrewmateDialog -> KiroCrewAgentsPage cycle.
 */
import { Loader2, MessageSquare, UserPen } from 'lucide-react'
import { Btn, SendBtn } from '../ui'
import {
  Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '../ui/dialog'
import ErrorBoundary from '../ErrorBoundary'
import ErrorNotice from '../ErrorNotice'
import CrewAvatar, { packAvatarFrom } from '../CrewAvatar'
import CrewStateAvatar from '../CrewStateAvatar'
import CrewAvatarBuilder from '../CrewAvatarBuilder'
import { retiredCueFrom, motionsFrom, soundsFrom } from '../../lib/crewAvatarState'
import CrewAvatarButton from './CrewAvatarButton'
import CrewWakeSection from '../CrewWakeSection'
import CrewWebhookSection from '../CrewWebhookSection'
import CrewEditorRail from './CrewEditorRail'
import CrewOverviewPane from './CrewOverviewPane'
import AgentTemplateDetail from './AgentTemplateDetail'
import CrewCapabilitiesPane from './CrewCapabilitiesPane'
import { SourceBadge } from '../SourceBadge'
import { effortLabel } from '../../lib/effort'
import { i18nT } from '../../i18n/t'
import {
  Field,
  WorkspaceModal,
  WorkspaceField,
  MemoryStoreField,
  memberMemoryState,
  ModelField,
  EffortField,
  DisplayNameField,
  TriggersField,
  SessionColorField,
} from '../../pages/KiroCrewAgentsPage'
import { INHERIT_MODEL, type CrewEditorController } from './useCrewEditor'

export default function CrewEditorDialog({ ctl }: { ctl: CrewEditorController }) {
  if (!ctl.open) {
    // The pill was clicked but the roster read has not resolved the record yet.
    // Render a minimal loading dialog so the click is not a silent dead one (a
    // failed read is surfaced by MembersPage's roster ErrorNotice, not here).
    if (ctl.loading) {
      return (
        <Dialog open onOpenChange={next => { if (!next) ctl.requestClose() }}>
          <DialogContent maxWidth={790} aria-busy data-testid="crew-editor-loading">
            {/* Radix requires a DialogTitle inside DialogContent for the modal's
                accessible name; visually hidden because the spinner is the only
                visible content. */}
            <DialogTitle className="sr-only">{i18nT('pages.chatPage.loading')}</DialogTitle>
            <div className="flex items-center justify-center gap-2 py-10 text-[13px] text-muted">
              <Loader2 size={16} className="lucide-inline animate-spin" />
              {i18nT('pages.chatPage.loading')}
            </div>
          </DialogContent>
        </Dialog>
      )
    }
    return null
  }
  const {
    editing, editingAgent,
    kiroAgent, setKiroAgent, workspace, setWorkspace, memoryStore,
    triggers, setTriggers, displayName, setDisplayName, sessionColor, setSessionColor,
    editModel, setEditModel, editEffort, setEditEffort, editAvatar,
    kiroAgentOptions, workspaceOptions, modelOptions, availableModels, templateProvenance,
    templateFieldLabel, kirocrewCfg, editorOptionsError, modelsDegraded, retryModels, retryingModels, pinHarness,
    resolved, resolvedError, effortCapable, effortLevels, effortModel,
    collidingCrews, sharingWorkspace, sharingMemoryStore,
    pane, requestPane, goToPane, panelId, sections, routingWords, templatePaneActive,
    wakeJobs, wakeUnknown, boundWebhooks, webhooksUnknown,
    capabilityManaged, capabilityReadFailed, capabilityLoading,
    setCapabilityDirty, setCapabilityBusy, onCapabilitiesSaved, setTemplateDirty,
    templateSwitchError, persistTemplateSwitch, onPaneSaveChain,
    avatarBuilderOpen, openAvatarBuilder, applyAvatar, closeAvatarBuilder, onAvatarImageError,
    sheetBusy, dirtyPanes, schedDraft, setSchedDraft, setSchedSaving, requestCancelDraft,
    capabilityDirty, capabilityBusy,
    confirmDelete, setConfirmDelete, deleteCrew,
    error, sheetHint, save, requestClose, requestChat,
    discardAsk, setDiscardAsk, discardTakesSheet, askSchedOnly, schedSaving, discardForce, confirmDiscard,
    wsModalOpen, openWsModal, closeWsModal, onWorkspaceCreated, setWsDirty,
  } = ctl

  return (
    <Dialog open onOpenChange={next => { if (!next) requestClose() }}>
      <DialogContent
        maxWidth={790}
        aria-label={i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: displayName.trim() || editing })}
      >
        <DialogHeader className="flex-wrap sm:flex-nowrap">
          <div className="flex w-full min-w-0 items-center gap-3 sm:w-auto sm:flex-1" data-testid="crew-editor-identity">
            <CrewAvatarButton
              size={28}
              onEdit={openAvatarBuilder}
              disabled={sheetBusy}
              data-testid="header-avatar-button"
            >
              <CrewStateAvatar seed={editing} avatar={editAvatar ?? undefined} size={28} onImageError={onAvatarImageError} />
            </CrewAvatarButton>
            <DialogTitle className="flex-1">
              <span className="font-mono">{displayName.trim() || editing}</span>
            </DialogTitle>
            {editingAgent?.source && <SourceBadge source={editingAgent.source} />}
          </div>
          <div className="ml-auto flex items-center gap-2" data-testid="crew-editor-actions">
            <Btn onClick={openAvatarBuilder} disabled={sheetBusy} data-testid="header-edit-avatar" title={i18nT('components.avatarBuilder.edit_avatar')} aria-label={i18nT('components.avatarBuilder.edit_avatar')}>
              <UserPen className="lucide-inline" aria-hidden="true" />
              <span className="hidden sm:inline">{i18nT('components.avatarBuilder.edit_avatar')}</span>
            </Btn>
            <Btn onClick={requestChat} title={i18nT('memoryV2.chat_member')} aria-label={i18nT('memoryV2.chat_member')}>
              <MessageSquare className="lucide-inline" aria-hidden="true" />
              <span className="hidden sm:inline">{i18nT('memoryV2.chat_member')}</span>
            </Btn>
          </div>
        </DialogHeader>

        {/* F2: a failed options read (installed templates / workspaces / config)
            must not present the ['kirocrew'] / ['default'] fallbacks as the real
            Agent-template and Workspace lists — a Save would then persist a
            binding the operator never chose. No hand-off: `dirtyPanes` sees only
            the editor's OWN panes, not the parent Crewmates page's Schedules
            create draft (and the ChatPane DM composer) that this modal left
            mounted behind it — and `ErrorNotice`'s "Ask the agent" navigates to
            /chat ungated (no NavigationLeaveGuard), so a clean editor over a
            dirty parent draft would still discard it. */}
        <ErrorNotice
          className="mx-5 mt-1"
          title={i18nT('pages.kiroCrewAgentsPage.editor_options_load_failed')}
          message={editorOptionsError ? (editorOptionsError instanceof Error ? editorOptionsError.message : String(editorOptionsError)) : null}
          askAgent={false}
          testId="crew-editor-options-load-error"
        />
        {/* The editor body is a plain scroll region rather than <DialogBody>:
            the rail and pane own their padding, so the body must sit flush
            (no px-5 py-4). Carries the primitive's scroll defaults
            (min-h-0 flex-1 overflow) plus the two-column layout. */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden sm:flex-row">
          <fieldset
            disabled={sheetBusy}
            aria-busy={sheetBusy}
            className={`contents ${sheetBusy ? '[&>*]:pointer-events-none [&>*]:opacity-60' : ''}`}
          >
            <CrewEditorRail
              sections={sections}
              value={pane}
              onChange={requestPane}
              ariaLabel={i18nT('components.crewEditor.rail_label')}
              unsavedLabel={i18nT('components.crewEditor.unsaved_changes')}
              sharedLabel={i18nT('components.crewEditor.tag_shared')}
              panelIdPrefix={panelId}
            />
            <div
              id={`${panelId}-${pane}`}
              role="tabpanel"
              aria-labelledby={`${panelId}-tab-${pane}`}
              tabIndex={-1}
              className={pane === 'capabilities'
                ? 'flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden'
                : 'flex min-w-0 flex-1 flex-col gap-3.5 overflow-y-auto px-5 py-4'}
            >
              {pane === 'overview' && (
                <>
                  <DisplayNameField value={displayName} onChange={setDisplayName} fallback={editing} />
                  <CrewOverviewPane
                    hub={
                      <CrewAvatarButton size={34} onEdit={openAvatarBuilder} data-testid="hub-avatar-button">
                        <CrewAvatar seed={editing} avatar={editAvatar ?? undefined} size={34} />
                      </CrewAvatarButton>
                    }
                    templateLabel={templateFieldLabel}
                    template={kiroAgent}
                    workspace={workspace}
                    memoryStore={memoryStore}
                    modelLabel={editModel === INHERIT_MODEL ? i18nT('pages.kiroCrewAgentsPage.inherited') : editModel}
                    modelInherited={editModel === INHERIT_MODEL}
                    resolvedModel={resolved?.model || ''}
                    activeSchedules={wakeJobs.filter(j => j.enabled).length}
                    schedulesUnknown={wakeUnknown}
                    routingWords={routingWords}
                    sharingCrews={collidingCrews.length}
                    workspaceShared={sharingWorkspace.length > 0}
                    memoryShared={sharingMemoryStore.length > 0}
                    webhookTokens={boundWebhooks}
                    webhooksUnknown={webhooksUnknown}
                    onNavigate={goToPane}
                  />
                </>
              )}

              <CrewCapabilitiesPane
                key={editing}
                member={editing}
                members={ctl.memberNames}
                hidden={pane !== 'capabilities'}
                onDirtyChange={setCapabilityDirty}
                onBusyChange={setCapabilityBusy}
                onSaved={onCapabilitiesSaved}
              />
              {pane === 'template' && (
                <ErrorBoundary scope="agent-template-pane" retryOnly>
                  <>
                    {/* No askAgent hand-off: it navigates to /chat, unmounting
                        this dialog and destroying its unsaved pane edits
                        (dirtyPanes). Errors inside the editor render in place,
                        never as a hand-off. */}
                    <ErrorNotice
                      message={templateSwitchError || (capabilityReadFailed ? i18nT('crewCapabilities.failed') : null)}
                      variant="inline"
                      testId="crew-template-switch-error"
                    />
                    <AgentTemplateDetail
                      actionsDisabled={capabilityDirty || capabilityBusy}
                      suppressHandoff
                      readOnly={capabilityManaged || capabilityDirty || capabilityBusy || capabilityLoading || capabilityReadFailed}
                      onCapabilities={() => requestPane('capabilities')}
                      template={kiroAgent}
                      models={(availableModels || []).map((m: { name: string }) => m.name).filter(Boolean)}
                      crew={editing || undefined}
                      onForked={setKiroAgent}
                      options={kiroAgentOptions}
                      onSelect={persistTemplateSwitch}
                      onRebound={setKiroAgent}
                      provenance={templateProvenance}
                      fieldLabel={templateFieldLabel}
                      onSaveChain={onPaneSaveChain}
                      onDirtyChange={setTemplateDirty}
                    />
                  </>
                </ErrorBoundary>
              )}

              {pane === 'model' && (
                <>
                  <ModelField
                    options={modelOptions}
                    value={editModel}
                    onChange={setEditModel}
                    servedAs={editModel === editingAgent?.model ? resolved?.pin_served_as : undefined}
                    harness={pinHarness}
                  />
                  {/* The model list resolved DEGRADED (transport failure → the
                      adapter returned an auto-only/cached list). Surfaced through
                      ErrorNotice per errors-use-error-notice, not a hand-rolled
                      status box. No hand-off: the "Ask the agent" navigate leaves
                      /members ungated and would discard the parent page's
                      Schedules create draft the modal left mounted. */}
                  {modelsDegraded && (
                    <div className="mt-1 flex flex-wrap items-center gap-2">
                      <ErrorNotice
                        className="min-w-[12rem] flex-1"
                        // Another harness's list keeps no last-good copy, so a failure leaves only Inherited.
                        message={resolved?.models_backend === undefined
                          ? i18nT('pages.chatSidebar.model_list_failed')
                          : i18nT('pages.kiroCrewAgentsPage.harness_models_failed', { harness: pinHarness })}
                        askAgent={false}
                        testId="crew-editor-models-degraded"
                      />
                      <Btn type="button" className="shrink-0" onClick={retryModels} disabled={retryingModels}>
                        {i18nT('pages.chatSidebar.retry')}
                      </Btn>
                    </div>
                  )}
                  {(effortCapable || !!editEffort) && (
                    <EffortField value={editEffort} onChange={setEditEffort} levels={effortLevels} />
                  )}
                  {!effortCapable && !!editEffort && (
                    <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                      {resolved?.effort_supported === false
                        ? i18nT('pages.kiroCrewAgentsPage.effort_ignored_by_this_backend', { harness: pinHarness })
                        : effortModel
                        ? i18nT('pages.kiroCrewAgentsPage.effort_ignored_on_this_model', { model: effortModel })
                        : i18nT('pages.kiroCrewAgentsPage.effort_pin_needs_a_model')}
                    </div>
                  )}
                  {/* No hand-off: the crew editor's unsaved pane edits
                      (dirtyPanes). Without this a failed resolve just left the
                      readout below absent, as if the crew had no model. */}
                  <ErrorNotice
                    message={resolvedError ? (resolvedError instanceof Error ? resolvedError.message : String(resolvedError)) : null}
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
              )}

              {pane === 'place' && (
                <>
                  <WorkspaceField
                    options={workspaceOptions}
                    value={workspace}
                    onChange={setWorkspace}
                    onNewWorkspace={openWsModal}
                    subject="agent"
                  />
                  <MemoryStoreField
                    value={memoryStore}
                    member={editing}
                    memoryState={memberMemoryState(editing, memoryStore, kirocrewCfg?.memory_stores)}
                    busy={sheetBusy || !kirocrewCfg}
                    manageDisabled={dirtyPanes.size > 0 || schedDraft}
                    onManage={() => ctl.navigateManageMemory()}
                  />
                  {editing === 'default' && collidingCrews.length > 0 && (
                    <div className="rounded-md border border-warn-subtle bg-warn-subtle px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                      {i18nT('pages.kiroCrewAgentsPage.also_used_by_these_crews', { crews: collidingCrews.join(', ') })}
                    </div>
                  )}
                </>
              )}

              {pane === 'schedules' && (
                <CrewWakeSection crew={editing} memberId={ctl.memberId} agentTemplate={kiroAgent} isDefaultCrew={ctl.isDefaultCrew} onDraftChange={setSchedDraft} onSavingChange={setSchedSaving} onRequestCancel={requestCancelDraft} onNavigateAway={ctl.guardedNavigate} />
              )}

              {pane === 'webhook' && <CrewWebhookSection crew={editing} onNavigateAway={ctl.guardedNavigate} />}

              {pane === 'routing' && (
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
              )}

              {pane === 'danger' && (
                <div className="flex flex-col gap-3 rounded-md border border-danger-subtle bg-danger-subtle p-3">
                  <p className="m-0 text-[12px] leading-relaxed text-muted">
                    {confirmDelete
                      ? i18nT('pages.kiroCrewAgentsPage.delete_crew_named_confirm', { name: editing })
                      : i18nT('pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its')}
                  </p>
                  <div ref={ctl.confirmRef} className="flex items-center gap-2">
                    <div className="flex-1" />
                    {confirmDelete ? (
                      <>
                        <Btn onClick={() => setConfirmDelete(false)} data-testid="cancel-delete-crew">{i18nT('pages.kiroCrewAgentsPage.keep_crew')}</Btn>
                        <Btn danger onClick={deleteCrew} disabled={sheetBusy} data-testid="confirm-delete-crew">
                          {i18nT('pages.kiroCrewAgentsPage.yes_delete_it')}
                        </Btn>
                      </>
                    ) : (
                      <Btn danger onClick={() => setConfirmDelete(true)} disabled={sheetBusy}>
                        {i18nT('pages.kiroCrewAgentsPage.delete_crew')}
                      </Btn>
                    )}
                  </div>
                </div>
              )}
            </div>
          </fieldset>
        </div>

        {!templatePaneActive && pane !== 'capabilities' && (
          <DialogFooter>
            {/* No hand-off: the crew editor's unsaved pane edits (dirtyPanes) —
                a failed save is exactly what did not persist them. */}
            <ErrorNotice message={error} variant="inline" className="mr-auto" testId="crew-sheet-error" />
            {sheetHint && <span className="mr-auto text-[12px] text-danger" data-testid="crew-sheet-hint">{sheetHint}</span>}
            {dirtyPanes.size > 0 && !error && !sheetHint && (
              <span className="mr-auto text-[11.5px] text-muted" data-testid="crew-unsaved-note">
                {capabilityDirty
                  ? i18nT('crewCapabilities.finishDraftFirst')
                  : schedDraft
                    ? i18nT('pages.kiroCrewAgentsPage.finish_the_new_schedule_first')
                    : i18nT('components.crewEditor.unsaved_changes')}
              </span>
            )}
            <Btn onClick={requestClose}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
            <SendBtn
              onClick={save}
              disabled={sheetBusy || dirtyPanes.size === 0 || schedDraft || capabilityDirty || capabilityBusy}
              title={capabilityDirty ? i18nT('crewCapabilities.finishDraftFirst') : schedDraft ? i18nT('pages.kiroCrewAgentsPage.finish_the_new_schedule_first') : undefined}
            >{i18nT('pages.kiroCrewAgentsPage.save_changes')}</SendBtn>
          </DialogFooter>
        )}

        <WorkspaceModal
          open={wsModalOpen}
          workspaceOptions={workspaceOptions}
          onCreated={onWorkspaceCreated}
          onClose={closeWsModal}
          onDirtyChange={setWsDirty}
          onSaveChain={onPaneSaveChain}
        />

        <Dialog open={discardAsk !== null} onOpenChange={next => { if (!next) setDiscardAsk(null) }}>
          <DialogContent
            maxWidth={440}
            className="z-[110]"
            aria-label={askSchedOnly
              ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')
              : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_changes')}
          >
            <DialogHeader>
              <DialogTitle className="whitespace-normal">
                {askSchedOnly
                  ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule')
                  : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_changes')}
              </DialogTitle>
            </DialogHeader>
            <DialogBody>
              <p className="m-0 text-sm text-text">
                {schedDraft
                  ? i18nT('pages.kiroCrewAgentsPage.discard_new_schedule_body')
                  : i18nT('pages.kiroCrewAgentsPage.discard_unsaved_body', { name: editing })}
              </p>
              {schedDraft && discardTakesSheet && (
                <p className="mb-0 mt-2 text-sm text-text" data-testid="crew-sched-discard-also-crew">
                  {i18nT('pages.kiroCrewAgentsPage.discard_also_crew_edits')}
                </p>
              )}
              {schedSaving && (
                <p className="mb-0 mt-2 text-[12px] text-muted" data-testid="crew-sched-discard-saving-note">
                  {discardForce
                    ? i18nT('pages.kiroCrewAgentsPage.discard_anyway_note')
                    : i18nT('pages.kiroCrewAgentsPage.discard_locked_while_saving')}
                </p>
              )}
            </DialogBody>
            <DialogFooter>
              <Btn onClick={() => setDiscardAsk(null)} data-testid="crew-sched-discard-keep">
                {i18nT('pages.kiroCrewAgentsPage.keep_editing')}
              </Btn>
              <Btn
                danger
                onClick={confirmDiscard}
                disabled={schedSaving && !discardForce}
                title={schedSaving ? i18nT('components.jobForm.saving') : undefined}
                data-testid="crew-sched-discard-confirm"
              >
                {askSchedOnly
                  ? i18nT('pages.kiroCrewAgentsPage.discard_schedule_confirm')
                  : i18nT('pages.kiroCrewAgentsPage.discard_confirm')}
              </Btn>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {editing && (
          <CrewAvatarBuilder
            open={avatarBuilderOpen}
            name={editing}
            value={editAvatar}
            retiredCue={retiredCueFrom(editingAgent?.avatar)}
            savedPack={packAvatarFrom(editingAgent?.avatar) !== null}
            savedReactions={{
              motions: motionsFrom(editingAgent?.avatar) !== null,
              sounds: soundsFrom(editingAgent?.avatar) !== null,
            }}
            onCancel={closeAvatarBuilder}
            onSave={applyAvatar}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}
