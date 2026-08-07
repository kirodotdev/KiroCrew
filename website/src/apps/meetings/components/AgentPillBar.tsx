// The row of agent toggles above a meeting's panels, plus the preset picker and
// the attachment menu.
//
// Each pill turns one agent on or off for THIS meeting. An enabled, unmuted
// agent shows a live dot while the meeting is running.

import { Paperclip, Plus, Settings2, Sparkles, Star, X } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import Clickable from '../../../components/Clickable'
import SimpleSelect from '../../../components/SimpleSelect'
import { Btn } from '../../../components/ui'
import type { AgentDef, Attachment, MeetingStatus, Preset } from '../api'

interface Props {
  agents: AgentDef[]
  enabledIds: string[]
  mutedAgents: string[]
  presets: Record<string, Preset>
  defaultPreset: string
  selectedPreset: string
  status: MeetingStatus
  attachments: Attachment[]
  attachMenuOpen: boolean
  onPresetChange: (name: string) => void
  onToggleAgent: (id: string, enable: boolean) => void
  onOpenSettings: () => void
  onToggleAttachMenu: () => void
  onAddAttachment: () => void
  onRemoveAttachment: (index: number) => void
}

export default function AgentPillBar({
  agents,
  enabledIds,
  mutedAgents,
  presets,
  defaultPreset,
  selectedPreset,
  status,
  attachments,
  attachMenuOpen,
  onPresetChange,
  onToggleAgent,
  onOpenSettings,
  onToggleAttachMenu,
  onAddAttachment,
  onRemoveAttachment,
}: Props) {
  const presetNames = Object.keys(presets)

  return (
    <div className="px-6 py-2 border-b border-border flex flex-wrap items-center gap-2">
      {presetNames.length > 0 ? (
        <SimpleSelect
          options={presetNames}
          // `presetDefaultOption` decorates only the preset the config marks as
          // default; the rest render bare. Labels stay positionally in lockstep
          // with `options`, so the VALUE handed to `onPresetChange` is always the
          // undecorated preset name.
          optionLabels={presetNames.map(name =>
            name === defaultPreset
              ? i18nT('apps.meetings.pillBar.presetDefaultOption', { name })
              : name,
          )}
          value={selectedPreset}
          onChange={onPresetChange}
          // Reproduces the old `<option value="">`: a selectable top row that
          // clears the selection back to '' and shows in the trigger while empty.
          clearLabel={i18nT('apps.meetings.pillBar.noPreset')}
          aria-label={i18nT('apps.meetings.pillBar.presetLabel')}
          style={{ minWidth: 160 }}
        />
      ) : (
        <Btn onClick={onOpenSettings}>
          <Plus className="lucide-inline" />
          {i18nT('apps.meetings.pillBar.createPreset')}
        </Btn>
      )}

      <span className="w-px h-5 bg-border mx-1" aria-hidden="true" />

      {agents.map(agent => {
        const enabled = enabledIds.includes(agent.id)
        const muted = mutedAgents.includes(agent.id)
        const title = muted
          ? i18nT('apps.meetings.pillBar.agentMuted', { name: agent.name })
          : enabled
            ? i18nT('apps.meetings.pillBar.disableAgent', { name: agent.name })
            : i18nT('apps.meetings.pillBar.enableAgent', { name: agent.name })
        return (
          <Btn
            key={agent.id}
            primary={enabled}
            onClick={() => onToggleAgent(agent.id, !enabled)}
            title={title}
            className={`rounded-full ${enabled ? '' : 'opacity-60 hover:opacity-100'}`}
          >
            <Sparkles className="lucide-inline" />
            {agent.name}
            {enabled && !muted && status === 'active' && (
              <span
                className="w-1.5 h-1.5 rounded-full bg-ok animate-pulse"
                aria-label={i18nT('apps.meetings.pillBar.listening')}
              />
            )}
          </Btn>
        )
      })}

      <Btn
        onClick={onOpenSettings}
        aria-label={i18nT('apps.meetings.pillBar.manageAgents')}
        title={i18nT('apps.meetings.pillBar.manageAgents')}
        className="rounded-full"
      >
        <Settings2 className="lucide-inline" />
      </Btn>

      <span className="w-px h-5 bg-border mx-1" aria-hidden="true" />

      <div className="relative">
        <Btn
          onClick={onToggleAttachMenu}
          aria-label={i18nT('apps.meetings.pillBar.manageAttachments')}
          aria-expanded={attachMenuOpen}
          className="rounded-full"
        >
          <Paperclip className="lucide-inline" />
          {attachments.length > 0 && <span className="font-medium">{attachments.length}</span>}
        </Btn>
        {attachMenuOpen && (
          <>
            {/* A pointer-only click-away scrim. It is `role="presentation"` and
                `aria-hidden`, so assistive tech never sees it as a control — the
                keyboard route out of the menu is Escape, handled below. Giving
                the scrim itself a keyboard affordance would announce a phantom
                button covering the whole viewport. */}
            <div
              className="fixed inset-0 z-10"
              role="presentation"
              aria-hidden="true"
              onClick={onToggleAttachMenu}
            />
            <div
              className="absolute top-full left-0 mt-1 w-64 bg-card border border-border rounded-lg shadow-lg z-20 py-1"
              role="menu"
              tabIndex={-1}
              aria-label={i18nT('apps.meetings.pillBar.attachmentsMenu')}
              onKeyDown={e => {
                if (e.key === 'Escape') onToggleAttachMenu()
              }}
            >
              {attachments.length > 0 ? (
                attachments.map((attachment, index) => (
                  <div
                    key={`${attachment.label}-${index}`}
                    className="flex items-center justify-between gap-2 px-3 py-1.5 text-[13px] hover:bg-bg-hover"
                  >
                    <span className="text-text truncate" title={attachment.url ?? attachment.path}>
                      {attachment.label}
                    </span>
                    <Btn
                      danger
                      onClick={() => onRemoveAttachment(index)}
                      aria-label={i18nT('apps.meetings.pillBar.removeAttachment', {
                        label: attachment.label,
                      })}
                    >
                      <X className="lucide-inline" />
                    </Btn>
                  </div>
                ))
              ) : (
                <div className="px-3 py-2 text-[13px] text-muted">
                  {i18nT('apps.meetings.pillBar.noAttachments')}
                </div>
              )}
              <div className="border-t border-border mt-1 pt-1 px-2 pb-1">
                <Clickable
                  onClick={onAddAttachment}
                  className="w-full text-left px-2 py-1 text-[13px] text-accent hover:bg-bg-hover rounded cursor-pointer"
                >
                  <Plus className="lucide-inline" />
                  {i18nT('apps.meetings.pillBar.addLink')}
                </Clickable>
              </div>
            </div>
          </>
        )}
      </div>

      {defaultPreset && selectedPreset === defaultPreset && (
        <span className="ml-auto text-[12px] text-muted inline-flex items-center gap-1">
          <Star className="lucide-inline" />
          {i18nT('apps.meetings.pillBar.usingDefaultPreset')}
        </span>
      )}
    </div>
  )
}
