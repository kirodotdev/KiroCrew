import { safeSetItem } from '../utils/safeStorage'
import { createSlice, PayloadAction } from '@reduxjs/toolkit'

export interface TerminalSession {
  id: string
  label: string
}

interface TerminalState {
  open: boolean
  position: 'bottom' | 'right'
  sessions: TerminalSession[]
  activeSessionId: string | null
}

const STORAGE_KEY = 'kirocrew-terminal'
const LABELS_KEY = 'kirocrew-terminal-labels'

function loadPersistedState(): Partial<TerminalState> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) return JSON.parse(raw)
  } catch { /* ignore */ }
  return {}
}

export function loadLabels(): Record<string, string> {
  try {
    const raw = localStorage.getItem(LABELS_KEY)
    if (raw) return JSON.parse(raw)
  } catch { /* ignore */ }
  return {}
}

export function saveLabels(labels: Record<string, string>) {
  try { safeSetItem(LABELS_KEY, JSON.stringify(labels)) } catch { /* ignore */ }
}

export function removeLabel(id: string) {
  const labels = loadLabels()
  delete labels[id]
  saveLabels(labels)
}

const persisted = loadPersistedState()

const initialState: TerminalState = {
  open: persisted.open ?? false,
  position: persisted.position ?? 'bottom',
  sessions: [],
  activeSessionId: null,
}

const terminalSlice = createSlice({
  name: 'terminal',
  initialState,
  reducers: {
    toggleCliPanel(state) {
      state.open = !state.open
      persist(state)
    },
    openCliPanel(state) {
      state.open = true
      persist(state)
    },
    closeCliPanel(state) {
      state.open = false
      persist(state)
    },
    setCliPanelPosition(state, action: PayloadAction<'bottom' | 'right'>) {
      state.position = action.payload
      persist(state)
    },
    addSession(state, action: PayloadAction<TerminalSession>) {
      state.sessions.push(action.payload)
      state.activeSessionId = action.payload.id
    },
    removeSession(state, action: PayloadAction<string>) {
      state.sessions = state.sessions.filter(s => s.id !== action.payload)
      if (state.activeSessionId === action.payload) {
        state.activeSessionId = state.sessions[0]?.id ?? null
      }
    },
    setActiveSession(state, action: PayloadAction<string>) {
      state.activeSessionId = action.payload
    },
    renameSession(state, action: PayloadAction<{ id: string; label: string }>) {
      const s = state.sessions.find(s => s.id === action.payload.id)
      if (s) s.label = action.payload.label
    },
    setSessions(state, action: PayloadAction<TerminalSession[]>) {
      state.sessions = action.payload
      state.activeSessionId = action.payload[0]?.id ?? null
    },
  },
})

function persist(state: TerminalState) {
  try {
    safeSetItem(STORAGE_KEY, JSON.stringify({
      open: state.open,
      position: state.position,
    }))
  } catch { /* ignore */ }
}

export const {
  toggleCliPanel,
  openCliPanel,
  closeCliPanel,
  setCliPanelPosition,
  addSession,
  removeSession,
  setActiveSession,
  renameSession,
  setSessions,
} = terminalSlice.actions

export default terminalSlice.reducer
