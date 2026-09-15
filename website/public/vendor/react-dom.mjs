// Vendor stub: re-exports ReactDOM from the host.
const m = window.__kirocrew_modules?.['react-dom']
if (!m) throw new Error('[vendor/react-dom] Host modules not initialized.')
export default m
export const { createPortal, flushSync, unstable_batchedUpdates } = m
