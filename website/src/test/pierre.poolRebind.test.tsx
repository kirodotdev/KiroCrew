// @vitest-environment happy-dom
/** Real-library canary for the premise behind generation-keyed remounts.
 *
 *  `@pierre/diffs/react` hands the `WorkerPoolContext` value to the core
 *  `File` instance ONCE, when the ref callback constructs it. Changing the
 *  context value afterwards re-renders the component but never re-supplies
 *  the manager, so a mounted surface keeps talking to a retired pool unless
 *  its `<File>` is remounted — which is what `PierreShell`'s `key={generation}`
 *  does. If a `@pierre/diffs` upgrade starts rebinding on context change this
 *  test fails and the keying can be reconsidered.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { File as CoreFile } from '@pierre/diffs'
import { File, WorkerPoolContext } from '@pierre/diffs/react'
import type { WorkerPoolManager } from '@pierre/diffs/worker'

/** The slice of a manager the core `File` touches at construction. */
function fakeManager(): WorkerPoolManager {
  return {
    subscribeToThemeChanges() {},
    unsubscribeToThemeChanges() {},
    isWorkingPool: () => false,
    isInitialized: () => true,
    getFileRenderOptions: () => ({}),
  } as unknown as WorkerPoolManager
}

const FILE = { name: 'a.ts', contents: 'const a = 1\n' }

describe('@pierre/diffs File binding to the worker pool context', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('captures the manager at construction and ignores later context values', () => {
    const instances: CoreFile[] = []
    const hydrate = vi.spyOn(CoreFile.prototype, 'hydrate').mockImplementation(function (this: CoreFile) {
      instances.push(this)
    })
    vi.spyOn(CoreFile.prototype, 'render').mockImplementation(() => undefined)
    vi.spyOn(CoreFile.prototype, 'cleanUp').mockImplementation(() => undefined)

    const first = fakeManager()
    const second = fakeManager()
    const view = render(
      <WorkerPoolContext.Provider value={first}>
        <File file={FILE} />
      </WorkerPoolContext.Provider>,
    )
    expect(hydrate).toHaveBeenCalledTimes(1)
    expect((instances[0] as unknown as { workerManager: unknown }).workerManager).toBe(first)

    view.rerender(
      <WorkerPoolContext.Provider value={second}>
        <File file={FILE} />
      </WorkerPoolContext.Provider>,
    )
    // Same instance, still bound to the retired manager: context alone does
    // not rebind. Only a remount (new key) constructs a `File` against `second`.
    expect(hydrate).toHaveBeenCalledTimes(1)
    expect((instances[0] as unknown as { workerManager: unknown }).workerManager).toBe(first)

    view.rerender(
      <WorkerPoolContext.Provider value={second}>
        <File key="next-generation" file={FILE} />
      </WorkerPoolContext.Provider>,
    )
    expect(hydrate).toHaveBeenCalledTimes(2)
    expect((instances[1] as unknown as { workerManager: unknown }).workerManager).toBe(second)
  })
})
