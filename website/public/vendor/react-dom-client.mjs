// Vendor stub: re-exports ReactDOM/client from the host.
const m = window.__kirocrew_modules?.['react-dom']
if (!m) throw new Error('[vendor/react-dom-client] Host modules not initialized.')
export const { createRoot, hydrateRoot } = m
