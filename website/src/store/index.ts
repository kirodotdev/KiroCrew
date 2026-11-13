import { configureStore, type Middleware } from '@reduxjs/toolkit'
import { useDispatch, useSelector, useStore } from 'react-redux'
import dashboardReducer from './dashboardSlice'
import notificationsReducer from './notificationsSlice'
import chatReducer from './chatSlice'
import instancesReducer from './instancesSlice'

/**
 * Hooks the render benchmark (scripts/bench-render.mjs) installs before the
 * bundle loads: a middleware that counts dispatches and a wrapper that counts
 * selector calls. Only the profiling build (scripts/vite.profiling.config.mjs)
 * sets VITE_KC_PERF, so every other build constant-folds `perfHooks` to
 * undefined and drops these seams entirely.
 */
type PerfHooks = {
  __kcPerfMw?: Middleware
  __kcSelWrap?: <S>(selector: S) => S
  __kcStore?: unknown
}
const perfHooks: PerfHooks | undefined =
  import.meta.env.VITE_KC_PERF === '1' && typeof window !== 'undefined'
    ? (window as unknown as PerfHooks)
    : undefined
const perfMiddleware = perfHooks?.__kcPerfMw

export const store = configureStore({
  middleware: (getDefaultMiddleware) =>
    perfMiddleware ? getDefaultMiddleware().concat(perfMiddleware) : getDefaultMiddleware(),
  reducer: {
    dashboard: dashboardReducer,
    notifications: notificationsReducer,
    chat: chatReducer,
    instances: instancesReducer,
  },
})
// The benchmark reads state to check that the frames it sent reached the store.
if (perfHooks) perfHooks.__kcStore = store

export type RootState = ReturnType<typeof store.getState>
export type AppDispatch = typeof store.dispatch
export type AppStore = typeof store
export const useAppDispatch = useDispatch.withTypes<AppDispatch>()
const rawUseAppSelector = useSelector.withTypes<RootState>()
const selectorWrap = perfHooks?.__kcSelWrap
export const useAppSelector: typeof rawUseAppSelector = selectorWrap
  ? (((selector: Parameters<typeof rawUseAppSelector>[0], equalityFn?: Parameters<typeof rawUseAppSelector>[1]) =>
      rawUseAppSelector(selectorWrap(selector), equalityFn)) as typeof rawUseAppSelector)
  : rawUseAppSelector
export const useAppStore = useStore.withTypes<AppStore>()
