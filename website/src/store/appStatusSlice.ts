import { createSlice, type PayloadAction } from '@reduxjs/toolkit'

/** A render-safe nav status for one app. `tone` is one of the core render
 *  tones (neutral/busy/positive/caution/critical); an unknown tone is treated
 *  as neutral by the render layer, so this slice stores it verbatim. */
export interface AppNavStatus {
  tone: string
  label: string
}

interface AppStatusState {
  byApp: Record<string, AppNavStatus>
}

const initialState: AppStatusState = { byApp: {} }

const appStatusSlice = createSlice({
  name: 'appStatus',
  initialState,
  reducers: {
    /** Live update from an `app_nav_status` WS frame. */
    setAppNavStatus(state, action: PayloadAction<{ app: string; tone: string; label?: string }>) {
      state.byApp[action.payload.app] = {
        tone: action.payload.tone,
        label: action.payload.label ?? '',
      }
    },
    /** Remove an app's status (app disabled/uninstalled, or explicit clear). */
    clearAppNavStatus(state, action: PayloadAction<string>) {
      delete state.byApp[action.payload]
    },
    /** Seed statuses from the initial `GET /api/apps` payload on load. */
    hydrateAppNavStatuses(state, action: PayloadAction<Record<string, AppNavStatus>>) {
      state.byApp = { ...action.payload }
    },
  },
})

export const { setAppNavStatus, clearAppNavStatus, hydrateAppNavStatuses } = appStatusSlice.actions

/** Read one app's nav status, or null when the app has reported none. Tolerates
 *  a store that has not registered this slice (returns null) so a partial test
 *  store — or a surface mounted before the reducer is wired — never throws. */
export const selectAppNavState = (
  state: { appStatus?: AppStatusState },
  app: string,
): AppNavStatus | null => state.appStatus?.byApp?.[app] ?? null

export default appStatusSlice.reducer
