/**
 * Widget complexity signal.
 *
 * Answers one question: is this widget's HTML heavy enough that the Tailwind v4
 * browser runtime's on-demand compile will be *perceptible*, so the frame should
 * show a progress indicator instead of sitting blank?
 *
 * Heuristic: count unique Tailwind utility tokens in class attributes. The v4
 * runtime compiles each unique class on first encounter, so the unique count —
 * not the element count or the byte size — is what tracks compile time.
 */

/** Above this many unique Tailwind utility classes, the compile is perceptible
 * and the frame shows a progress indicator. */
export const TAILWIND_COMPLEXITY_THRESHOLD = 50

/**
 * Tailwind utility prefixes used to decide whether a class token is one the JIT
 * must compile. Author-defined classes (`panel`, `kb-label`) cost nothing, so
 * counting them would inflate the signal.
 */
const TW_UTILITY_PREFIXES = [
  // Layout
  'flex', 'grid', 'block', 'inline', 'hidden', 'absolute', 'relative',
  'fixed', 'sticky', 'static', 'inset', 'top-', 'right-', 'bottom-', 'left-',
  // Sizing
  'w-', 'h-', 'min-w-', 'min-h-', 'max-w-', 'max-h-', 'size-',
  // Spacing
  'p-', 'px-', 'py-', 'pt-', 'pr-', 'pb-', 'pl-', 'm-', 'mx-', 'my-',
  'mt-', 'mr-', 'mb-', 'ml-', 'gap-', 'space-',
  // Typography
  'text-', 'font-', 'leading-', 'tracking-', 'truncate', 'line-clamp',
  // Colors / backgrounds
  'bg-', 'border-', 'ring-', 'shadow', 'opacity-',
  // Flex / grid children
  'items-', 'justify-', 'self-', 'place-', 'col-', 'row-', 'order-',
  // Borders
  'rounded', 'border', 'divide-',
  // Effects
  'transition', 'duration-', 'ease-', 'animate-', 'transform', 'scale-',
  'rotate-', 'translate-',
  // Interactivity
  'cursor-', 'select-', 'pointer-events-',
  // Overflow
  'overflow-', 'overscroll-',
  // Stacking
  'z-',
  // Variant prefixes — each variant is a separate compile
  'sm:', 'md:', 'lg:', 'xl:', '2xl:', 'dark:', 'hover:', 'focus:', 'active:',
  'group-', 'peer-',
]

/** Every `class`/`className` attribute value in the HTML. */
function extractClassValues(html: string): string[] {
  const classRe = /\bclass(?:Name)?="([^"]*)"/gi
  const results: string[] = []
  let m: RegExpExecArray | null
  while ((m = classRe.exec(html)) !== null) {
    results.push(m[1])
  }
  return results
}

function isTailwindUtility(token: string): boolean {
  // Arbitrary values (`bg-[#fff]`, `w-[200px]`) are always JIT-compiled.
  if (token.includes('[') && token.includes(']')) return true
  for (const prefix of TW_UTILITY_PREFIXES) {
    if (token === prefix || token.startsWith(prefix)) return true
  }
  return false
}

export interface ComplexityResult {
  /** Unique Tailwind utility classes the JIT will have to compile. */
  tailwindClassCount: number
  /** Whether the frame should show a progress indicator while compiling. */
  needsProgressIndicator: boolean
}

/**
 * Measure how much Tailwind compilation a widget implies.
 *
 * Intentionally narrow. It does NOT decide whether to skip the Tailwind runtime:
 * this is a STATIC scan of class attributes and cannot see classes a widget's
 * own <script> adds at runtime, so acting on it to drop the runtime would render
 * a dynamically-built widget (Chart.js, D3) completely unstyled.
 */
export function analyzeWidgetComplexity(html: string): ComplexityResult {
  const uniqueTw = new Set<string>()
  for (const classStr of extractClassValues(html)) {
    for (const token of classStr.split(/\s+/)) {
      if (token && isTailwindUtility(token)) uniqueTw.add(token)
    }
  }
  return {
    tailwindClassCount: uniqueTw.size,
    needsProgressIndicator: uniqueTw.size > TAILWIND_COMPLEXITY_THRESHOLD,
  }
}
