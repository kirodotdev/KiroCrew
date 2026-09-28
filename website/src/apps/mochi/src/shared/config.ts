/**
 * Mochi - AppConfig type definitions and defaults
 */
import type { ColorMap } from './colorCustomizer'
import type { CatPreset } from './catPresets'

export type BackendMode = 'local' | 'cloud-desktop'

/** Structured MCP server configuration with per-agent assignment and tool policies. */
export interface McpServerConfig {
  name: string
  agents: ('chat' | 'bg')[]
  autoApprove: string[]
  disabledTools: string[]
}

/** Servers that default to both chat and bg agents when added as plain strings. */
export const BOTH_AGENT_DEFAULTS = new Set(['slack-mcp', 'ai-community-slack-mcp'])

/** Normalize a plain string or partial McpServerConfig into a full McpServerConfig. */
export function normalizeMcpServerConfig(entry: string | McpServerConfig): McpServerConfig {
  if (typeof entry === 'string') {
    const agents: ('chat' | 'bg')[] = BOTH_AGENT_DEFAULTS.has(entry) ? ['chat', 'bg'] : ['chat']
    return { name: entry, agents, autoApprove: [], disabledTools: [] }
  }
  return {
    name: entry.name,
    agents: Array.isArray(entry.agents) && entry.agents.length > 0
      ? entry.agents.filter(a => a === 'chat' || a === 'bg')
      : (BOTH_AGENT_DEFAULTS.has(entry.name) ? ['chat', 'bg'] : ['chat']),
    autoApprove: Array.isArray(entry.autoApprove) ? entry.autoApprove : [],
    disabledTools: Array.isArray(entry.disabledTools) ? entry.disabledTools : [],
  }
}

export interface AppConfig {
  agentBackend: {
    url: string       // default: "http://localhost:7777"
    wsUrl: string     // default: "ws://localhost:7777/api/ws"
    mode: BackendMode // default: "local"
    /** Unused: remote-gateway mode is not part of this build. */
    cloudDesktopHost: string
  }
  shortcuts: {
    voiceInput: string    // legacy fallback — actual PTT uses Right Option via hotkey-monitor
    screenCapture: string // default: "CommandOrControl+Shift+X"
    toggleWindow: string  // default: "CommandOrControl+Shift+P"
    hideAll: string       // default: "CommandOrControl+Shift+H"
  }
  voice: {
    backend: 'apple' | 'whisper'
    whisperModelPath?: string
    enablePolish: boolean
    polishPrompt?: string
    language?: string
  }
  trust: {
    level: 'normal' | 'trust_reads' | 'trust' | 'yolo'
    toolOverrides: Record<string, 'ask' | 'auto'>
  }
  features: {
    clipboardMonitor: boolean   // default: false
    accessibilityContext: boolean // default: true
  }
  window: {
    position: { x: number; y: number }
    visible: boolean
    expanded: boolean
    chatAlwaysOnTop: boolean
  }
  pet: {
    character: string  // default: 'mochiCat'
  }
  llm: {
    useAgentBackend: boolean  // default: true — reuse KiroCrew's LLM
    customEndpoint?: string
    customApiKey?: string
  }
  memory: {
    maxMemories: number  // default: 10
  }
  notifications: {
    sound: boolean
    style: 'bubble' | 'native' | 'both'
  }
  mochi: {
    language: string              // preferred response language, default: 'English'
    petName: string               // pet's name, default: 'Mochi'
    theme: string                 // UI theme: 'mocha' | 'sakura' | 'original' | 'teal' | 'terracotta' | 'periwinkle', default: 'mocha'
    quietPeriodMins: number       // don't re-notify within this window, default: 5
    activityTier: 'economy' | 'balanced' | 'active' | 'unlimited'  // background-agent spend tier, default: 'balanced'
    bgModel: string               // model override for background runs, '' = agent default
    breakReminderMins: number     // suggest break after this much work, default: 60
    activityMode: 'active' | 'normal' | 'quiet'  // pet behavior mode, default: 'normal'
    activityLogMaxEntries: number // summarize & clear when exceeded, default: 500
    soul: string                  // custom soul/personality prompt, default: '' (uses built-in default)
    activeAppearance: string      // active appearance pack id, default: 'default-mochi'
    restoreSessions: boolean      // restore chat history on startup, default: true
    sessionHistoryDays: number    // max age of restorable history in days, default: 7
    colorMaps: Record<string, ColorMap>  // packId → colorMap, default: {}
    customPresets: CatPreset[]           // user-created presets, default: []
    extraMcpServers: (string | McpServerConfig)[]  // additional MCP servers, supports plain strings (backward compat) or structured config
    firstLaunchDone: boolean      // set to true after first welcome message, default: false
    silentSubagents: boolean      // suppress bg agent completion notifications, default: false
    autoCompactEnabled: boolean   // auto-compact when context usage exceeds threshold, default: true
    autoCompactThresholdPct: number // context usage % to trigger auto-compact, default: 60
    /**
     * Which gateway's Mochi the one pet shows: 'self' (this computer, default) or
     * an instance id from core's `GET /api/instances`. Stored on the LOCAL
     * instance and never synced — one pet is a per-machine resource, so the
     * pointer is a property of this machine. Kept OPAQUE (not validated against
     * the live list) so a saved choice survives an instance that is temporarily
     * away; resolution falls back to 'self'. See builtins/mochi/settings.py.
     */
    petInstance: string
  }
}

export const DEFAULT_CONFIG: AppConfig = {
  agentBackend: {
    url: 'http://localhost:7777',
    wsUrl: 'ws://localhost:7777/api/ws',
    mode: 'local',
    cloudDesktopHost: '',
  },
  shortcuts: {
    voiceInput: 'Option+Space',
    screenCapture: 'CommandOrControl+Shift+X',
    toggleWindow: 'CommandOrControl+Shift+M',
    hideAll: 'CommandOrControl+Shift+H',
  },
  voice: {
    backend: 'apple',
    enablePolish: true,
  },
  trust: {
    level: 'normal',
    toolOverrides: {},
  },
  features: {
    clipboardMonitor: false,
    accessibilityContext: true,
  },
  window: {
    position: { x: 100, y: 100 },
    visible: true,
    expanded: false,
    chatAlwaysOnTop: true,
  },
  pet: {
    character: 'mochiCat',
  },
  llm: {
    useAgentBackend: true,
  },
  memory: {
    maxMemories: 10,
  },
  notifications: {
    sound: true,
    style: 'both',
  },
  mochi: {
    language: 'English',
    petName: 'Mochi',
    theme: 'mocha',
    quietPeriodMins: 5,
    activityTier: 'balanced',
    bgModel: '',
    breakReminderMins: 60,
    activityMode: 'normal' as const,
    activityLogMaxEntries: 500,
    soul: '',
    activeAppearance: 'default-mochi',
    restoreSessions: true,
    sessionHistoryDays: 7,
    colorMaps: {},
    customPresets: [],
    extraMcpServers: [],
    firstLaunchDone: false,
    silentSubagents: false,
    autoCompactEnabled: true,
    autoCompactThresholdPct: 60,
    // SELF_INSTANCE in builtins/mochi/settings.py — the local gateway.
    petInstance: 'self',
  },
}

/** Map activity mode to planning agent behavior instruction */
export function planningInstructionForMode(mode: AppConfig['mochi']['activityMode']): string {
  switch (mode) {
    case 'active':
      return 'Owner likes an active pet. Move frequently, share observations, give tips and encouragement. Be chatty and playful.'
    case 'quiet':
      return 'Owner prefers minimal interruption. Move rarely, only notify for important events (meetings, battery). Stay calm and unobtrusive.'
    default:
      return 'Balance activity and quiet. Move occasionally, notify for meaningful events only. Be helpful but not noisy.'
  }
}

/** Deep merge: apply partial overrides onto base config */
export function mergeConfig(base: AppConfig, overrides: Partial<AppConfig>): AppConfig {
  return {
    agentBackend: { ...base.agentBackend, ...overrides.agentBackend },
    shortcuts: { ...base.shortcuts, ...overrides.shortcuts },
    voice: { ...base.voice, ...overrides.voice },
    trust: {
      ...base.trust,
      ...overrides.trust,
      toolOverrides: {
        ...base.trust.toolOverrides,
        ...(overrides.trust?.toolOverrides ?? {}),
      },
    },
    features: { ...base.features, ...overrides.features },
    window: {
      ...base.window,
      ...overrides.window,
      position: { ...base.window.position, ...overrides.window?.position },
    },
    pet: { ...base.pet, ...overrides.pet },
    llm: { ...base.llm, ...overrides.llm },
    memory: { ...base.memory, ...overrides.memory },
    notifications: { ...base.notifications, ...overrides.notifications },
    mochi: {
      ...base.mochi,
      ...overrides.mochi,
      colorMaps: {
        ...base.mochi.colorMaps,
        ...(overrides.mochi?.colorMaps ?? {}),
      },
      customPresets: overrides.mochi?.customPresets ?? base.mochi.customPresets,
      extraMcpServers: overrides.mochi?.extraMcpServers !== undefined
        ? overrides.mochi.extraMcpServers.map(normalizeMcpServerConfig)
        : base.mochi.extraMcpServers.map(normalizeMcpServerConfig),
    },
  }
}


/**
 * Keyboard-accelerator constants for the shortcut recorder. These hold Electron
 * accelerator IDENTIFIERS (`CommandOrControl`, `Super`) and glyphs, not
 * translatable copy — they live here (an i18n-gate-ignored module) so the
 * strict gate does not read them as untranslated UI strings.
 */

/** Recorder glyph -> Electron accelerator token. `Alt` is portable; `Option` is not. */
export const ELECTRON_MAP: Record<string, string> = {
  '⌘': 'CommandOrControl',
  Ctrl: 'CommandOrControl',
  Win: 'Super',
  Alt: 'Alt',
  Shift: 'Shift',
}

export const MODIFIER_GLYPHS = ['⌘', 'Win', 'Ctrl', 'Alt', 'Shift']

/**
 * Canonical modifier order for the emitted accelerator. The recorder collects
 * keys in PRESS order, so sorting makes the stored string a function of the
 * chord rather than of typing order — the shell compares the configured
 * accelerator against what it registered, so two spellings of one chord would
 * otherwise read as drift forever.
 */
export const MODIFIER_ORDER = ['CommandOrControl', 'Super', 'Alt', 'Shift']

/** The pet's product/brand name. Not translatable; mirrors backend soul_loader.DEFAULT_PET_NAME. */
export const PRODUCT_NAME = 'Mochi'
