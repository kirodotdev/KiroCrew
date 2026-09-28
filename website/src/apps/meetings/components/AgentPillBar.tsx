// The row of agent toggles above a meeting's panels, plus the preset picker and
// the attachment menu.
//
// Each pill turns one agent on or off for THIS meeting. An enabled, unmuted
// agent shows a live dot while the meeting is running.

import { useEffect, useRef } from 'react'
import { Paperclip, Plus, Settings2, Sparkles, Star, X } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import SimpleSelect from '../../../components/SimpleSelect'
import { Btn } from '../../../components/ui'
import { menuItemsOf, useMenuKeyboard } from '../../../hooks/useMenuKeyboard'
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

  // The attachment menu is `role="menu"`, so it owes the WAI-ARIA menu keyboard
  // contract: arrows/Home/End move real focus between the items, Tab stays inside
  // the open menu, and focus enters on open. `useMenuKeyboard` carries all of it
  // (#6231); the items are the menu's actual CONTROLS (each row's remove button,
  // the footer action) — a row <div> is layout, not a stop.
  const menuRef = useRef<HTMLDivElement>(null)
  // The trigger, so Escape can hand focus back (see the Escape branch below).
  const attachBtnRef = useRef<HTMLButtonElement>(null)
  useMenuKeyboard({ enabled: attachMenuOpen, containerRef: menuRef })

  // Focus entry owes a matching REPAIR, because this component is controlled:
  // activating an item changes nothing here, the HOST changes a prop on a later
  // render, and two of those renders pull the DOM out from under the focused
  // element:
  //   (a) a removal keeps the menu OPEN and refreshes `attachments`, so the
  //       index-keyed row holding focus unmounts. Focus falls to <body>, from
  //       where Tab escapes the still-open menu and the Escape branch below is
  //       unreachable (it is bound to the menu element, not the document).
  //   (b) a successful 'Add a link' CLOSES the menu from the host with focus
  //       still inside it, orphaning focus on <body> rather than returning it
  //       to the paperclip the way an explicit Escape does.
  // A plain effect, NOT the mochi-ContextMenu restore-on-unmount shape: this
  // component stays mounted across both paths, so there is no unmount to hang
  // the restore on — the prop change IS the event.
  const prevMenuOpenRef = useRef(attachMenuOpen)
  useEffect(() => {
    const wasOpen = prevMenuOpenRef.current
    prevMenuOpenRef.current = attachMenuOpen
    // Adopt ONLY focus that is genuinely lost. Anything else — the trigger, the
    // composer, a still-live menu item, another pane — is somewhere the user or
    // the browser deliberately put focus, and moving it would be the theft this
    // repair exists to prevent.
    if (document.activeElement !== document.body) return
    if (attachMenuOpen) {
      // Still open: put the keyboard back on the first surviving item (the
      // trigger only as a fallback for a menu that has no items at all).
      ;(menuItemsOf(menuRef.current)[0] ?? attachBtnRef.current)?.focus()
    } else if (wasOpen) {
      // Restore to the trigger ONLY on an actual close. Gated on the previous
      // render having been open so an unrelated `attachments` refresh while the
      // menu is shut — and the very first render, where nothing was focused yet
      // — cannot yank focus onto the paperclip out of nowhere.
      attachBtnRef.current?.focus()
    }
  }, [attachments, attachMenuOpen])

  return (
    <div className="px-4 md:px-6 py-2 border-b border-border flex flex-wrap items-center gap-2">
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
          ref={attachBtnRef}
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
              ref={menuRef}
              className="absolute top-full left-0 mt-1 w-64 bg-card border border-border rounded-lg shadow-lg z-20 py-1"
              role="menu"
              tabIndex={-1}
              aria-label={i18nT('apps.meetings.pillBar.attachmentsMenu')}
              onKeyDown={e => {
                if (e.key === 'Escape') {
                  onToggleAttachMenu()
                  // `useMenuKeyboard` moved focus INTO the menu when it opened, so
                  // the rows are about to unmount from under the keyboard user.
                  // Hand focus back to the paperclip — the same dismissal posture
                  // MenuBtn takes (focus in on open, back to the trigger on
                  // explicit dismissal); without it focus would be orphaned on
                  // <body> with no obvious way back into the bar.
                  attachBtnRef.current?.focus()
                }
              }}
            >
              {attachments.length > 0 ? (
                attachments.map((attachment, index) => (
                  <div
                    key={`${attachment.label}-${index}`}
                    // Plain layout, deliberately NOT `role="menuitem"`: the row
                    // has no activation of its own, and `menuitem` subclasses
                    // `command`, so labelling it would announce an item whose
                    // Enter/Space does nothing — the same promise-not-kept
                    // defect #6231 is fixing. The menu semantics sit on the
                    // control below, which the hook discovers directly.
                    className="flex items-center justify-between gap-2 px-3 py-1.5 text-[13px] hover:bg-bg-hover"
                  >
                    <span className="text-text truncate" title={attachment.url ?? attachment.path}>
                      {attachment.label}
                    </span>
                    <Btn
                      danger
                      // The actual menu item: a native <button> (so Enter/Space
                      // activate it for free) wearing `menuitem` so assistive
                      // tech announces it as an item OF this menu rather than a
                      // loose button inside one. `menuItemsOf` finds it either
                      // way; the role is for the announcement.
                      role="menuitem"
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
                {/* A native <button role="menuitem">, not `Clickable`: role="menu"
                    owns only menuitem / menuitemradio / menuitemcheckbox / group
                    / separator, and `Clickable` hardcodes role="button" (and Omits
                    `role` from its props), which made this footer an INVALID owned
                    child — assistive tech announced a loose button sitting inside
                    the menu instead of an item OF it, and the menu's item count
                    was short by one. A plain <button> carries the same free
                    Enter/Space activation `Clickable` was providing, so the swap
                    costs nothing; the classes below reproduce Clickable's look
                    (`bg-transparent border-none` undoes the native chrome, the
                    same spelling MicSourceMenu's rows use). */}
                <button
                  type="button"
                  role="menuitem"
                  onClick={onAddAttachment}
                  className="w-full text-left px-2 py-1 text-[13px] text-accent hover:bg-bg-hover rounded cursor-pointer bg-transparent border-none"
                >
                  <Plus className="lucide-inline" />
                  {i18nT('apps.meetings.pillBar.addLink')}
                </button>
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
