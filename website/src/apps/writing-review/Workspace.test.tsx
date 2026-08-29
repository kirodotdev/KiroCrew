/**
 * Contract tests for ``Workspace`` -- the two-column shell that owns
 * the sidebar (ReviewList + new-review button) and the detail pane.
 *
 * Three code paths in the detail-pane render:
 *
 * 1. ``activeJobId`` set → renders ``ScanProgress``.
 * 2. ``activeJobId`` null AND ``selectedReviewId`` set → renders
 *    ``ReviewDetail``.
 * 3. Neither set → renders ``EmptyState``.
 *
 * Plus two button behaviours: the New Review button dispatches
 * ``openNewReviewDialog`` and disables itself while a scan is in
 * flight (so a user can't kick off a second scan while one is still
 * running).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

vi.mock('./context', () => ({
  useWritingReview: vi.fn(),
}))
// Child components are stubbed so this test asserts only ``Workspace``'s
// own branching logic, not the children's implementations. Each child is
// individually covered by its own test file.
vi.mock('./components/EmptyState', () => ({
  default: () => <div data-testid="stub-empty-state" />,
}))
vi.mock('./components/NewReviewDialog', () => ({
  default: () => <div data-testid="stub-new-review-dialog" />,
}))
vi.mock('./components/ReviewDetail', () => ({
  default: () => <div data-testid="stub-review-detail" />,
}))
vi.mock('./components/ReviewList', () => ({
  default: () => <div data-testid="stub-review-list" />,
}))
vi.mock('./components/ScanProgress', () => ({
  default: () => <div data-testid="stub-scan-progress" />,
}))
vi.mock('./components/SettingsPanel', () => ({
  default: () => <div data-testid="stub-settings-panel" />,
}))

import Workspace from './Workspace'
import { useWritingReview } from './context'

const mockedUseWritingReview = vi.mocked(useWritingReview)

function makeFakeContextValueForWorkspace(
  overrides: {
    activeJobId?: string | null
    selectedReviewId?: string | null
    newReviewDialogOpen?: boolean
    settingsDialogOpen?: boolean
  } = {},
) {
  const openNewReviewDialog = vi.fn()
  const openSettingsDialog = vi.fn()
  const contextValue = {
    activeJobId: overrides.activeJobId ?? null,
    selectedReviewId: overrides.selectedReviewId ?? null,
    newReviewDialogOpen: overrides.newReviewDialogOpen ?? false,
    settingsDialogOpen: overrides.settingsDialogOpen ?? false,
    openNewReviewDialog,
    openSettingsDialog,
    // The other context fields are unused by Workspace itself; supplying
    // stubs keeps the type discriminant satisfied for TypeScript while
    // the test focuses on the branching Workspace actually does.
    selectReview: vi.fn(),
    closeNewReviewDialog: vi.fn(),
    closeSettingsDialog: vi.fn(),
    setActiveJobId: vi.fn(),
    activeJobDocName: null,
    setActiveJobDocName: vi.fn(),
    activeJobPhase: null,
    setActiveJobPhase: vi.fn(),
    reviewsQuery: {},
    reviewDetailQuery: {},
    settingsQuery: {},
  }
  return { contextValue, openNewReviewDialog, openSettingsDialog }
}

describe('Workspace', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders ScanProgress when a scan is in flight', () => {
    const { contextValue } = makeFakeContextValueForWorkspace({
      activeJobId: 'job-abc',
      selectedReviewId: 'review-xyz',
    })
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    render(<Workspace />)
    expect(screen.getByTestId('stub-scan-progress')).toBeInTheDocument()
    expect(screen.queryByTestId('stub-review-detail')).toBeNull()
    expect(screen.queryByTestId('stub-empty-state')).toBeNull()
  })

  it('renders ReviewDetail when a review is selected and no scan is running', () => {
    const { contextValue } = makeFakeContextValueForWorkspace({
      selectedReviewId: 'review-xyz',
    })
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    render(<Workspace />)
    expect(screen.getByTestId('stub-review-detail')).toBeInTheDocument()
    expect(screen.queryByTestId('stub-scan-progress')).toBeNull()
    expect(screen.queryByTestId('stub-empty-state')).toBeNull()
  })

  it('renders EmptyState when nothing is selected and no scan is running', () => {
    const { contextValue } = makeFakeContextValueForWorkspace()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    render(<Workspace />)
    expect(screen.getByTestId('stub-empty-state')).toBeInTheDocument()
    expect(screen.queryByTestId('stub-scan-progress')).toBeNull()
    expect(screen.queryByTestId('stub-review-detail')).toBeNull()
  })

  it('mounts NewReviewDialog when newReviewDialogOpen is true', () => {
    const { contextValue } = makeFakeContextValueForWorkspace({
      newReviewDialogOpen: true,
    })
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    render(<Workspace />)
    expect(screen.getByTestId('stub-new-review-dialog')).toBeInTheDocument()
  })

  it('mounts SettingsPanel when settingsDialogOpen is true', () => {
    const { contextValue } = makeFakeContextValueForWorkspace({
      settingsDialogOpen: true,
    })
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    render(<Workspace />)
    expect(screen.getByTestId('stub-settings-panel')).toBeInTheDocument()
  })

  it('dispatches openNewReviewDialog when the New Review button is clicked', () => {
    const { contextValue, openNewReviewDialog } = makeFakeContextValueForWorkspace()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    render(<Workspace />)
    // The New Review button carries a Plus icon and localised label; the
    // easiest robust match is by role + first-of-two buttons in the
    // sidebar (Settings is the sibling gear icon).
    const buttonElements = screen.getAllByRole('button')
    // The first button in the sidebar is the primary "New Review" action;
    // it is not disabled here because activeJobId is null.
    const newReviewButton = buttonElements.find(button => !button.hasAttribute('disabled'))
    expect(newReviewButton).toBeDefined()
    fireEvent.click(newReviewButton!)
    expect(openNewReviewDialog).toHaveBeenCalledTimes(1)
  })

  it('disables the New Review button while a scan is in flight', () => {
    const { contextValue, openNewReviewDialog } = makeFakeContextValueForWorkspace({
      activeJobId: 'job-inflight',
    })
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    render(<Workspace />)
    // The New Review button MUST be disabled — kicking off a second scan
    // while one is running would clobber the active-job pointer and
    // orphan the in-flight review.
    const disabledButton = screen.getAllByRole('button').find(
      button => button.hasAttribute('disabled'),
    )
    expect(disabledButton).toBeDefined()
    fireEvent.click(disabledButton!)
    // A disabled button click MUST NOT dispatch the handler even if the
    // click event still fires (React ignores it, but this pins the
    // behaviour so a future refactor to a non-native button component
    // doesn't silently re-enable the double-click.)
    expect(openNewReviewDialog).toHaveBeenCalledTimes(0)
  })
})
