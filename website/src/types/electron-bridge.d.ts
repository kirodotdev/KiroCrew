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
    /** Watch how far the cursor travels OUTSIDE this window while a focus-mode
     *  reveal is open. Fires the callback once — `true` past the dismissal
     *  distance, `false` if the cursor came back inside — and returns an
     *  unsubscribe that also disarms the main-process poll. Optional: absent in a
     *  browser and on a shell older than the feature. */
    watchCursorAway?: (cb: (away: boolean) => void) => (() => void) | undefined
    setDevMode: (enabled: boolean) => void
    getAppMenuItems: (id: string) => Promise<ElectronAppMenuItem[]>
    executeAppMenuItem: (id: string, index: number) => void
    onNavigate: (cb: (path: string) => void) => () => void
    /** View > Toggle Focus Mode. Optional: absent on a shell older than the menu item. */
    onToggleFocusMode?: (cb: () => void) => () => void
    /** A macOS swipe the shell relays as one route-history step (-1 Back,
     *  1 Forward). Optional: absent in a browser and on an older shell. */
    onHistoryStep?: (cb: (delta: unknown) => void) => () => void
    /** `menuBarOverlap` is false when macOS placed the fullscreen window below
     *  the menu bar (a notched display), so no band needs keeping clear. */
    onFullScreenChanged: (cb: (isFullScreen: boolean, menuBarOverlap: boolean) => void) => () => void
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

  type CloseAction = 'background' | 'quit'

  interface CloseActionAPI {
    get: () => Promise<CloseAction>
    set: (action: CloseAction) => Promise<CloseAction>
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

  /**
   * The OS-native folder chooser (desktop shell only). Neither call takes an
   * argument: the dialog's options are fixed in the main process.
   */
  interface FolderPickAPI {
    /** Whether this window's gateway runs on this machine, so a pick can work. */
    available: () => Promise<boolean>
    /** The chosen absolute folder, or null when the user cancels; rejects when no dialog could be shown. */
    pick: () => Promise<string | null>
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

  /** One picked element as the in-page overlay reports it. `ref` is the
   *  agent-visible element ref (the same `eN` `snapshot` mints). The NOTE is
   *  not here: it is typed and kept in the panel, never in the page realm. */
  interface BrowserAnnotationTarget {
    id: number
    /** 1-based marker number shown on the page. */
    n: number
    ref: string
    tag: string
    role: string
    name: string
    text: string
    selector: string
    /** The element left the document since it was annotated (or the page it
     *  was on navigated away). */
    detached: boolean
  }

  type BrowserAnnotateFailure = { ok: false; code: string; error: string }

  interface BrowserAnnotatePoll {
    ok: true
    picking: boolean
    url: string
    title: string
    items: BrowserAnnotationTarget[]
    /** One-shot: an element was just picked -- open the note editor for it. */
    picked?: number
    /** One-shot: a marker on the page was clicked -- edit that note. */
    edit?: number
  }

  interface BrowserAnnotateCapture {
    ok: true
    /** Base64 PNG of the viewport with the numbered markers rendered in. */
    png: string
    url: string
    title: string
  }

  type BrowserAnnotateOp = 'start' | 'stop' | 'poll' | 'remove' | 'clear' | 'capture' | 'teardown'

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
    /** Element annotation on the native page. Optional: absent on a desktop
     *  shell older than the feature, which is what hides the button. */
    annotate?: (
      panelId: string,
      op: BrowserAnnotateOp,
      args?: Record<string, unknown>,
    ) => Promise<
      | BrowserAnnotateFailure
      | BrowserAnnotatePoll
      | BrowserAnnotateCapture
      | { ok: true; url?: string; title?: string }
      | null
    >
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
    // Set with code 'gateway-still-running': the command that stops the
    // gateway the install was refused for.
    stopCommand?: string
    percent?: number
    bytesPerSecond?: number
    installHandoff?: 'windows-installer' | 'automatic-relaunch'
    laneVersion?: string
    runningAheadOfLane?: boolean | null
    replayed?: boolean
    /** The main process's confirmed governance verdict — true only when the
     *  local bundled gateway's policy provider was accepted via the three-gate
     *  assertLocalDashboard check. False for remote (SSH-tunneled) gateways. */
    policyGoverned?: boolean
  }

  interface UpdateInfo {
    version?: string
    channel?: string
    stampedChannel?: string | null
    channelSwitchable?: boolean
    // The organization's policy sets the channel (an enterprise build).
    channelLockedByPolicy?: boolean
    // The channel the policy requires, "" when none.
    requiredChannel?: string
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
    /** The main process's confirmed policy governance verdict — true only when
     *  the local bundled gateway's policy provider was accepted via the
     *  three-gate assertLocalDashboard check. */
    policyGoverned?: boolean
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
    /** Notify the main process that a security policy provider now governs (or
     *  no longer governs) this install's updates. Fire-and-forget; no return.
     *  Absent on shells that predate the feature. */
    notifyPolicyGoverned?: (governed: boolean) => void
  }

  interface Window {
    kirocrew?: KiroCrewBridge
    electronAPI?: ElectronAPI
    localGatewayAPI?: LocalGatewayAPI
    closeActionAPI?: CloseActionAPI
    crashReportsAPI?: CrashReportsAPI
    wslAPI?: WslAPI
    folderPickAPI?: FolderPickAPI
    zoomAPI?: ZoomAPI
    browserAPI?: BrowserAPI
    updateAPI?: UpdateAPI
  }
}

export {}
