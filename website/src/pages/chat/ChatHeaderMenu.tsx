import { useMemo, useState } from 'react'
import type React from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, MessageSquarePlus, Plug } from 'lucide-react'

import { api } from '../../api/client'
import SessionActionsMenu from '../../components/SessionActionsMenu'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import { deriveLoadedMcpTools } from '../../lib/mcpLoadedTools'
import { useAppSelector } from '../../store'
import type { McpServer } from '../../types'
import McpToolsPanel from './McpToolsPanel'

/** The session header's dropdown: reveal, rename, auto-title, mode, the MCP
 *  servers submenu and the rest of SessionActionsMenu. Header chrome, so it
 *  lives beside the page, not in the message-content module. */
export function ChatHeaderMenu({ activeSlot, agent, onReveal, onRename, onAutoTitle, mode, sidebarOnScreen, omitPopout, triggerLabel, newSessionHere }: {
  activeSlot: string | null; agent?: string; onReveal?: () => void; onRename?: () => void; onAutoTitle?: () => void; mode?: string
  /** Whether the sidebar (and its folder-order banner) is on screen -- see SessionActionsMenu. */
  sidebarOnScreen?: boolean
  /** See SessionActionsMenu: the phone bar's ⋯ menu owns the pop-out rows. */
  omitPopout?: boolean
  /** Phone single top bar: render the session TITLE inside the trigger, ahead of
   *  the chevron, so title and menu are one control (one tap target, chevron
   *  flush after the last character). Absent, the trigger is the bare chevron
   *  the desktop title row places beside its own rename control. */
  triggerLabel?: React.ReactNode
  /** Phone only: a first item that opens a sibling session in the on-screen
   *  session's folder. It lives in this menu, not beside it, because the phone
   *  bar's centre cell holds two controls (AUTOSDE `max-two-buttons-per-row`). */
  newSessionHere?: { label: string; disabled?: boolean; onSelect: () => void }
}) {
  // Controlled open state: lets the colour-swatch row (not a Radix menu item)
  // close the menu after a pick, via the onColorPicked hook passed below.
  const [open, setOpen] = useState(false)
  // MCP server list is fetched lazily when its submenu opens (driven by the
  // Radix Sub's open state).
  const [mcpOpen, setMcpOpen] = useState(false)
  const { data: servers = [] } = useQuery<{ name: string; enabled?: boolean }[]>({
    queryKey: ['mcp-servers', agent],
    queryFn: () => api.mcpActive(agent || undefined),
    enabled: mcpOpen,
  })
  // Tool Search mode for this session's MCP tools (shared ['kirocrewConfig']
  // cache). When on, tool specs are deferred (search-and-call), so every server
  // shows as connected but its tools load only when used; when off, every spec
  // is sent each turn. Explains the "why are they all loaded?" question.
  const { data: toolSearchOn = true } = useQuery<{ agent?: { tool_search?: boolean } }, Error, boolean>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    select: (c) => c.agent?.tool_search ?? true,
    enabled: mcpOpen,
  })
  // Per-tool loaded/deferred state is derived client-side (no endpoint): the
  // full server list carries each server's tool names + disabledTools, and the
  // "loaded this session" set comes from scanning this slot's tool_search
  // results in the chat store. See deriveLoadedMcpTools for the caveats.
  const { data: fullServers = [] } = useQuery<McpServer[]>({
    queryKey: ['mcp-servers-full'],
    queryFn: () => api.mcpServers(),
    enabled: mcpOpen,
  })
  const toolsByServer = useMemo(
    () => Object.fromEntries(fullServers.map(s => [s.name, { tools: s.tools, disabledTools: s.disabledTools }])),
    [fullServers],
  )
  const sessionMessages = useAppSelector(s => s.chat.messages)
  const loadedTools = useMemo(() => deriveLoadedMcpTools(sessionMessages), [sessionMessages])
  // What this slot's session actually reported about its MCP servers. Pushed by
  // the gateway onto the same `slots` array the snapshot rehydrates, so it needs
  // no query of its own — and unlike the ['mcp-servers'] query above it is keyed
  // by SLOT, which is the whole point: that query answers a question about the
  // agent's configuration and this answers one about this session.
  const sessionReport = useAppSelector(
    s => (activeSlot ? s.dashboard.slots?.find(sl => sl.key === activeSlot)?.mcp_report : null) ?? null,
  )

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        {triggerLabel !== undefined ? (
          /* No aria-label here: the visible title IS the accessible name, and the
             menu's role is appended as sr-only text -- an aria-label would replace
             the title, which on the phone this trigger is the only copy of. */
          <button data-testid="session-title-menu" className="flex min-w-0 items-center gap-1 px-1 py-1 rounded-md text-text-strong cursor-pointer bg-transparent border-none transition-colors hover:bg-bg-hover" aria-haspopup="menu">
            <span className="session-header-title text-[15px] font-semibold truncate min-w-0">{triggerLabel}</span>
            <span className="sr-only">, {i18nT('pages.chatPage.session_options')}</span>
            <ChevronDown size={16} className="shrink-0 text-muted" />
          </button>
        ) : (
          <button className="px-0.5 py-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none transition-all" aria-label={i18nT('pages.chatPage.session_options')}>
            <ChevronDown size={14} />
          </button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[180px]">
        {newSessionHere && (
          <>
            <DropdownMenuItem data-testid="mobile-new-session-here" disabled={newSessionHere.disabled} onSelect={newSessionHere.onSelect}>
              <MessageSquarePlus size={13} className="shrink-0 text-muted" /> {newSessionHere.label}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
          </>
        )}
        {activeSlot && (
        <SessionActionsMenu
          variant="dropdown"
          slotKey={activeSlot}
          mode={mode}
          sidebarOnScreen={sidebarOnScreen}
          omitPopout={omitPopout}
          // MCP servers: stateful (lazy fetch gated on the sub's open state), so
          // it stays here as an info slot rather than a generic capability.
          infoSlots={[
            <DropdownMenuSub key="mcp" onOpenChange={setMcpOpen}>
              <DropdownMenuSubTrigger>
                <Plug size={13} className="shrink-0 text-muted" />
                <span className="flex-1">{i18nT('pages.chatPage.mcp_servers')}</span>
                <ChevronRight size={12} className="text-muted" />
              </DropdownMenuSubTrigger>
              {/* Intentional tighter cap composed via min() with the primitive's
                  available-height var: 340px keeps the MCP tools submenu compact
                  while preserving the viewport never-clip floor (a bare max-h
                  would override the primitive, since cn()'s tailwind-merge dedupes
                  max-h-*). overflow is left to the primitive. */}
              <DropdownMenuSubContent className="min-w-[240px] max-w-[300px] max-h-[min(340px,var(--radix-dropdown-menu-content-available-height))] px-3 py-2">
                <McpToolsPanel
                  servers={servers}
                  toolsByServer={toolsByServer}
                  loaded={loadedTools}
                  toolSearchOn={toolSearchOn}
                  loading={servers.length === 0}
                  sessionReport={sessionReport}
                />
              </DropdownMenuSubContent>
            </DropdownMenuSub>,
          ]}
          onReveal={onReveal}
          onAutoTitle={onAutoTitle}
          onRename={onRename}
          // The header controls its own menu, so close it after a colour pick.
          onColorPicked={() => setOpen(false)}
        />
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
