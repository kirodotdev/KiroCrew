// Vendor stub: re-exports jsx runtime from the host.
const m = window.__kirocrew_modules?.['react/jsx-runtime']
if (!m) throw new Error('[vendor/react-jsx-runtime] Host modules not initialized.')
export const { jsx, jsxs, jsxDEV, Fragment } = m
