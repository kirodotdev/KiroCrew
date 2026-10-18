// The base props that most ChatInput.test.tsx and ChatInput.busyAndUndo.test.tsx
// cases spread.
import { vi } from 'vitest'

export const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}
