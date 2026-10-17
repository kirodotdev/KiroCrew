// Vendor stub: re-exports ReactDOM from the host.
// Names every runtime export of the pinned package (appSharedModuleBoundary.test.ts).
const m = window.__kirocrew_modules?.['react-dom']
if (!m) throw new Error('[vendor/react-dom] Host modules not initialized.')
export default m
export const {
  createPortal, flushSync, unstable_batchedUpdates, createRoot, hydrateRoot,
  render, hydrate, findDOMNode, unmountComponentAtNode,
  unstable_renderSubtreeIntoContainer, version,
  __SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED,
} = m
