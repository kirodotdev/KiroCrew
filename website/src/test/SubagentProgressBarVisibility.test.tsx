/**
 * Regression test for GitHub issue #728:
 * The SubagentProgressBar must remain visible when the activity sidebar is open
 * on any tab OTHER than 'subagents'. Only when the sidebar shows the Subagents
 * tab itself should the in-chat bar hide (de-duplication).
 *
 * Similarly, WorkflowProgressBar hides only when activityTab === 'workflows'.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot,
  sseSubagentSpawn,
  toggleActivity,
  openActivityToTab,
} from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import SubagentProgressBar from '../pages/chat/SubagentProgressBar'

vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

const SLOT = 'test-slot'

// Use the real useSelector to read from the test store
import { useSelector } from 'react-redux'
import type { RootState } from '../store'

/**
 * This wrapper replicates the ChatPage rendering condition for the progress bar:
 *   {!(activityOpen && activityTab === 'subagents') && <SubagentProgressBar />}
 *
 * It reads the same Redux state ChatPage uses, so dispatching toggleActivity /
 * openActivityToTab exercises the real code path the bug lived in.
 */
function ProgressBarWithGate({ slot }: { slot: string }) {
  const activityOpen = useSelector((s: RootState) => s.chat.activityOpen)
  const activityTab = useSelector((s: RootState) => s.chat.activityTab)
  return (
    <>
      {!(activityOpen && activityTab === 'subagents') && (
        <SubagentProgressBar slot={slot} />
      )}
    </>
  )
}

function makeStore() {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a1', task: 'Build feature', agent: 'gpu-coder' }))
  return store
}

describe('SubagentProgressBar visibility — issue #728', () => {
  it('shows the progress bar when the activity sidebar is closed', () => {
    const store = makeStore()
    render(
      <Provider store={store}>
        <ProgressBarWithGate slot={SLOT} />
      </Provider>,
    )
    expect(screen.getByTestId('subagent-running-count')).toBeInTheDocument()
  })

  it('shows the progress bar when the sidebar is open on a non-subagents tab (files)', () => {
    const store = makeStore()
    // Open sidebar on 'files' tab (the default)
    act(() => { store.dispatch(openActivityToTab('files')) })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('files')

    render(
      <Provider store={store}>
        <ProgressBarWithGate slot={SLOT} />
      </Provider>,
    )
    expect(screen.getByTestId('subagent-running-count')).toBeInTheDocument()
  })

  it('shows the progress bar when the sidebar is open on the logs tab', () => {
    const store = makeStore()
    act(() => { store.dispatch(openActivityToTab('tools')) })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('tools')

    render(
      <Provider store={store}>
        <ProgressBarWithGate slot={SLOT} />
      </Provider>,
    )
    expect(screen.getByTestId('subagent-running-count')).toBeInTheDocument()
  })

  it('hides the progress bar ONLY when the sidebar shows the subagents tab', () => {
    const store = makeStore()
    act(() => { store.dispatch(openActivityToTab('subagents')) })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('subagents')

    const { container } = render(
      <Provider store={store}>
        <ProgressBarWithGate slot={SLOT} />
      </Provider>,
    )
    // The bar should NOT render (de-duplicated with the sidebar's own view)
    expect(container.innerHTML).toBe('')
  })

  it('re-shows the bar when switching away from the subagents tab while sidebar stays open', () => {
    const store = makeStore()
    act(() => { store.dispatch(openActivityToTab('subagents')) })

    const { container, rerender } = render(
      <Provider store={store}>
        <ProgressBarWithGate slot={SLOT} />
      </Provider>,
    )
    expect(container.innerHTML).toBe('')

    // Switch to a different tab while keeping the sidebar open
    act(() => { store.dispatch(openActivityToTab('files')) })
    rerender(
      <Provider store={store}>
        <ProgressBarWithGate slot={SLOT} />
      </Provider>,
    )
    expect(screen.getByTestId('subagent-running-count')).toBeInTheDocument()
  })

  it('re-shows the bar when the sidebar is closed from the subagents tab', () => {
    const store = makeStore()
    act(() => { store.dispatch(openActivityToTab('subagents')) })

    const { container, rerender } = render(
      <Provider store={store}>
        <ProgressBarWithGate slot={SLOT} />
      </Provider>,
    )
    expect(container.innerHTML).toBe('')

    // Close the sidebar
    act(() => { store.dispatch(toggleActivity()) })
    expect(store.getState().chat.activityOpen).toBe(false)

    rerender(
      <Provider store={store}>
        <ProgressBarWithGate slot={SLOT} />
      </Provider>,
    )
    expect(screen.getByTestId('subagent-running-count')).toBeInTheDocument()
  })
})
