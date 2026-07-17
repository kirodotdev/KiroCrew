import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import Modal from './Modal'
import ProjectPicker from './ProjectPicker'
import { Btn, Input } from './ui'

interface Props {
  open: boolean
  onClose: () => void
  onSuccess: () => void
}

export default function ScanProjectsModal({ open, onClose, onSuccess }: Props) {
  const [scanPath, setScanPath] = useState('')
  const [scanResult, setScanResult] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout>>()

  useEffect(() => () => clearTimeout(timerRef.current), [])

  const scanMut = useMutation({
    mutationFn: (paths: string[]) => api.agentsRescan(paths),
    onSuccess: (data) => {
      setScanResult(`Discovered ${data.discovered} project agent(s)`)
      onSuccess()
      timerRef.current = setTimeout(() => { setScanResult(null); handleClose() }, 2000)
    },
    onError: (err: Error) => setScanResult(`Error: ${err.message}`),
  })

  const handleClose = () => { clearTimeout(timerRef.current); onClose(); setScanPath(''); setScanResult(null) }

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Scan for project agents"
      maxWidth={480}
      footer={<>
        <Btn onClick={handleClose}>Cancel</Btn>
        <Btn primary onClick={() => scanPath.trim() && scanMut.mutate([scanPath.trim()])} disabled={!scanPath.trim() || scanMut.isPending}>
          {scanMut.isPending ? 'Scanning…' : 'Scan'}
        </Btn>
      </>}
    >
      <div className="space-y-4 text-[13px] text-text">
        <p>KiroCrew will scan the directory you provide for <code className="text-[12px] bg-bg-hover px-1 py-0.5 rounded">.kiro/agents/</code> folders and register any agents it finds. Registered agents appear in the dropdown grouped under the project folder name.</p>
        <p className="text-muted">You can provide a parent directory (e.g. <code className="text-[12px] bg-bg-hover px-1 py-0.5 rounded">~/Documents</code>) to scan multiple projects at once, or a specific project path. The scan goes up to 8 levels deep and stops automatically at 50,000 directories to keep it fast.</p>
        <div>
          {/* Control is correctly associated via htmlFor+id; label-has-for's nesting requirement is a false positive here. */}
          {/* eslint-disable-next-line jsx-a11y/label-has-for */}
          <label htmlFor="scan-projects-path" className="block text-[11px] font-medium text-muted mb-1.5 uppercase tracking-wide">Directory to scan</label>
          <div className="flex gap-2">
            <Input
              id="scan-projects-path"
              ref={inputRef}
              className="flex-1 text-[13px]"
              placeholder="~/Documents or /path/to/my-project"
              value={scanPath}
              onChange={e => setScanPath(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && scanPath.trim() && !scanMut.isPending) scanMut.mutate([scanPath.trim()]) }}
              autoFocus
            />
            <Btn onClick={() => setPickerOpen(true)} title="Browse filesystem">Browse…</Btn>
          </div>
          <ProjectPicker
            open={pickerOpen}
            onOpenChange={setPickerOpen}
            anchorRef={inputRef}
            onSelect={path => { setScanPath(path); setPickerOpen(false) }}
          />
          <p className="text-[11px] text-muted mt-1">Tip: use <code className="bg-bg-hover px-1 py-0.5 rounded">~</code> for your home directory.</p>
        </div>
        {scanResult && <p className={`text-[12px] ${scanResult.startsWith('Error') ? 'text-danger' : 'text-ok'}`}>{scanResult}</p>}
      </div>
    </Modal>
  )
}
