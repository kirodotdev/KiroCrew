import { forwardRef, type InputHTMLAttributes } from 'react'

export interface ComposerAttachProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'onChange' | 'value' | 'defaultValue'> {
  onFiles?: (files: File[]) => void
}

/** Shared picker atom. Hosts keep their own trigger, focus return, accept hint
 *  and staging writer; both the dashboard and app embed use this input.
 *  A cancelled picker emits nothing. Reset after a pick allows the same file
 *  to be selected again after removal or a failed upload. */
export const ComposerAttach = forwardRef<HTMLInputElement, ComposerAttachProps>(function ComposerAttach({ onFiles, ...props }, ref) {
  return (
    <input
      {...props}
      ref={ref}
      type="file"
      data-composer-attach=""
      onChange={event => {
        const files = Array.from(event.target.files ?? [])
        if (files.length) onFiles?.(files)
        event.target.value = ''
      }}
    />
  )
})
