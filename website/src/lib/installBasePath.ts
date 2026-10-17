/**
 * Side-effect entry: install the base-path transport and DOM shims before any other
 * module runs, so nothing captures the unwrapped `fetch`. A no-op in the stock
 * root-mounted build. See ./basePath.ts.
 */
import { installBasePathDomShims, installBasePathShims } from './basePath'

installBasePathShims()
installBasePathDomShims()
