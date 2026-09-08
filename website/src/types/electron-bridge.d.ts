/**
 * Type contract for the dashboard bridges exposed by electron/preload.js.
 *
 * Every property is optional because the same renderer bundle also runs in a
 * browser, where no Electron preload is present.
 */

declare global {
  interface KiroCrewBridge {
    platform?: string
    isElectron?: boolean
    linuxFrameless?: boolean
    getPathForFile?: (file: File) => string
    windowControl?: (action: string) => void
  }

  interface ElectronMemorySample {
    realm: string
    usedHeapKB: number | null
    limitHeapKB: number | null
    externalKB: number | null
  }

  interface ElectronMenuSeparator {
    type: 'separator'
    index: number
  }

  interface ElectronMenuItem {
    type: 'normal' | 'checkbox' | 'radio'
    index: number
    label: string
    accelerator: string
    enabled: boolean
    checked: boolean
  }

  type ElectronAppMenuItem = ElectronMenuSeparator | ElectronMenuItem

  interface ElectronGlobalHotkeyInfo {
    accelerator: string
    default: string
  }

  interface ElectronAPI {
    onStatus: (cb: (msg: unknown) => void) => () => void
    onBootReady: (cb: () => void) => () => void
    bootComplete: () => void
    setThemeAccent: (hex: string) => void
    setThemeMode: (pref: string) => void
    setTitleBarOverlayTheme: (mode: string) => void
    setFocusModeChrome: (visible: boolean) => void
    setDevMode: (enabled: boolean) => void
    getAppMenuItems: (id: string) => Promise<ElectronAppMenuItem[]>
    executeAppMenuItem: (id: string, index: number) => void
    onNavigate: (cb: (path: string) => void) => () => void
    onFullScreenChanged: (cb: (isFullScreen: boolean) => void) => () => void
    setBadgeCount: (count: number) => void
    reportMicDenied: () => void
    reportMemorySample: (sample: ElectronMemorySample) => void
    heapStatisticsKB: () => { usedHeapKB: number | null } | null
    getGlobalHotkey: () => Promise<ElectronGlobalHotkeyInfo>
    /** Evict the HTTP cache of one loopback pane origin; resolves to whether a purge ran. */
    clearPaneHttpCache?: (origin: string) => Promise<boolean>
  }

  interface LocalGatewayAPI {
    get: () => Promise<boolean>
    set: (enabled: boolean) => Promise<boolean>
  }

  interface CrashReportsAPI {
    get: () => Promise<{ newCount: number }>
    reveal: () => Promise<{ ok: boolean; error?: string }>
  }

  interface WslDistro {
    name: string
    state: 'running' | 'stopped' | 'unknown'
    stateLabel: string
    version: number
    isDefault: boolean
  }

  interface WslDetectResult {
    available: boolean
    distros: WslDistro[]
    defaultDistro: string | null
    error?: string
    reason?: string
  }

  interface WslAPI {
    detect: () => Promise<WslDetectResult>
  }

  interface ZoomAPI {
    get: () => Promise<number>
    set: (factor: number) => Promise<number>
    step: (direction: 1 | -1) => Promise<number>
  }

  interface NativeBrowserState {
    open: boolean
    visible: boolean
    url: string
    bounds: { x: number; y: number; width: number; height: number } | null
    overlayActive?: boolean
    refused?: boolean
  }

  interface NativeBrowserEvent {
    panelId?: string
    url: string
    title: string
  }

  interface BrowserAPI {
    open: (panelId: string, url: string) => Promise<NativeBrowserState | null>
    navigate: (panelId: string, url: string) => Promise<NativeBrowserState | null>
    setBounds: (
      panelId: string,
      rect: { x: number; y: number; width: number; height: number },
      viewport: { width: number; height: number },
    ) => Promise<NativeBrowserState | null>
    setOverlayActive: (panelId: string, active: boolean) => Promise<NativeBrowserState | null>
    setInactive: (panelId: string, inactive: boolean) => Promise<NativeBrowserState | null>
    close: (panelId: string) => Promise<NativeBrowserState | null>
    getState: (panelId: string) => Promise<NativeBrowserState | null>
    setAgentAct: (panelId: string, enabled: boolean) => Promise<{ ok: boolean } | null>
    setControlOwner: (panelId: string, owner: string) => Promise<unknown>
    getControl: (panelId: string) => Promise<unknown>
    control: (panelId: string, operation: string, args: unknown) => Promise<unknown>
    trackSession: (panelId: string, tracked: boolean) => Promise<unknown>
    onAgentOpened: (cb: (event: NativeBrowserEvent) => void) => () => void
    onDidNavigate: (cb: (event: NativeBrowserEvent) => void) => () => void
    onTitleUpdated: (cb: (event: NativeBrowserEvent) => void) => () => void
  }

  interface UpdateState {
    state: 'checking' | 'found' | 'available' | 'downloading' | 'downloaded' | 'installing' | 'not-available' | 'error'
    version?: string
    notes?: string
    pubDate?: string
    channel?: string
    message?: string
    phase?: 'check' | 'download' | 'install'
    code?: string
    httpStatus?: number
    percent?: number
    bytesPerSecond?: number
    installHandoff?: 'windows-installer' | 'automatic-relaunch'
    laneVersion?: string
    runningAheadOfLane?: boolean | null
    replayed?: boolean
  }

  interface UpdateInfo {
    version?: string
    channel?: string
    stampedChannel?: string | null
    channelSwitchable?: boolean
    channelPreference?: string
    autoDownload?: boolean
    platform?: string
    downloadUrl?: string | null
    packaged?: boolean
    disabled?: string
    managedBy?: string
    updateCommand?: string
    laneVersion?: string
    runningAheadOfLane?: boolean | null
  }

  interface UpdateResult {
    ok: boolean
    error?: string
    info?: UpdateInfo
  }

  interface UpdateAPI {
    onState: (cb: (state: UpdateState) => void) => () => void
    check: () => Promise<UpdateResult>
    download?: () => Promise<UpdateResult>
    install: () => Promise<UpdateResult>
    getInfo?: () => Promise<(UpdateInfo & { lastState?: UpdateState | null }) | undefined>
    setChannel?: (channel: string) => Promise<UpdateResult>
    setAutoDownload?: (enabled: boolean) => Promise<UpdateResult>
  }

  interface Window {
    kirocrew?: KiroCrewBridge
    electronAPI?: ElectronAPI
    localGatewayAPI?: LocalGatewayAPI
    crashReportsAPI?: CrashReportsAPI
    wslAPI?: WslAPI
    zoomAPI?: ZoomAPI
    browserAPI?: BrowserAPI
    updateAPI?: UpdateAPI
  }
}

export {}
