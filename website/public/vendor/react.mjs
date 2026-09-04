// Vendor stub: re-exports React from the host's shared module registry.
// App bundles import 'react' → import map resolves to this file → reads from host.
const m = window.__kirocrew_modules?.react
if (!m) throw new Error('[vendor/react] Host modules not initialized. Ensure shared-modules.ts is loaded.')
export default m
export const {
  useState, useEffect, useRef, useCallback, useMemo, useContext, useReducer,
  useLayoutEffect, useImperativeHandle, useDebugValue, useDeferredValue,
  useTransition, useId, useSyncExternalStore, useInsertionEffect,
  createContext, createElement, cloneElement, createRef, forwardRef,
  lazy, memo, startTransition, Fragment, Suspense, StrictMode,
  Children, Component, PureComponent, isValidElement,
} = m
