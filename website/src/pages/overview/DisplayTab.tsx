import { Monitor, Sun, Moon, Pencil } from 'lucide-react'
import { useZoomCtx } from '../../hooks/ZoomProvider'
import { useTheme } from '../../hooks/useTheme'
import { Card, CardTitle } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { useThemeEditor, ThemeEditorPanel } from '../../components/themeEditor'

const BTN = 'px-3 py-1 rounded-full text-[13px] cursor-pointer border transition-all'
const active = (on: boolean) => on ? 'bg-accent-subtle text-accent border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'

export default function DisplayTab() {
  const { zoom, zoomSupported, zoomIn, zoomOut, reset, family, setFontFamily } = useZoomCtx()
  const { preference, setTheme, colorTheme, setColorTheme, allThemes } = useTheme()
  const editor = useThemeEditor()
  const modKey = /mac/i.test(navigator.platform) ? '⌘' : 'Ctrl'

  return (
    <div className="grid grid-cols-2 gap-4 max-[900px]:grid-cols-1">
      <Card>
        <CardTitle>Zoom <InfoTip text={zoomSupported ? `Native window zoom, the same setting as ${modKey}+ / ${modKey}−. Click the percentage to reset to 100%.` : 'Use your browser\u2019s zoom. Your browser remembers it for this site.'} /></CardTitle>
        {zoomSupported ? (
          <div className="flex items-center gap-2">
            <button className={BTN + ' ' + active(false)} onClick={zoomOut}>−</button>
            <button className={BTN + ' ' + active(false)} onClick={reset}>{zoom}%</button>
            <button className={BTN + ' ' + active(false)} onClick={zoomIn}>+</button>
          </div>
        ) : (
          <div className="text-[13px] text-muted">Zoom with {modKey} + / {modKey} −</div>
        )}
      </Card>
      <Card>
        <CardTitle>Font <InfoTip text="Change the dashboard font family. Persists across sessions." /></CardTitle>
        <div className="flex items-center gap-2">
          {(['sans', 'mono', 'system'] as const).map(f => (
            <button key={f} className={BTN + ' ' + active(family === f)} onClick={() => setFontFamily(f)}>
              {f === 'sans' ? 'Sans' : f === 'mono' ? 'Mono' : 'System'}
            </button>
          ))}
        </div>
      </Card>
      <Card>
        <CardTitle>Mode <InfoTip text="Switch between color schemes. Auto follows your OS preference." /></CardTitle>
        <div className="flex items-center gap-2">
          {(['system', 'light', 'dark'] as const).map(t => (
            <button key={t} className={BTN + ' ' + active(preference === t)} onClick={() => setTheme(t)}>
              {t === 'system' ? <><Monitor className="lucide-inline" /> Auto</> : t === 'light' ? <><Sun className="lucide-inline" /> Light</> : <><Moon className="lucide-inline" /> Dark</>}
            </button>
          ))}
        </div>
      </Card>
      <Card>
        <CardTitle>Color Theme <InfoTip text="Choose a color palette. Each theme supports dark and light modes. Custom themes are stored in ~/.kirocrew/themes/." /></CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          {allThemes.map(t => (
            <div key={t.value} className="relative group">
              <button className={BTN + ' ' + active(colorTheme === t.value)} onClick={() => setColorTheme(t.value)}>
                {t.label}
              </button>
              {t.custom && (
                <button
                  onClick={(e) => { e.stopPropagation(); editor.openEditTheme(t.value.replace('custom-', '')) }}
                  className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-accent text-accent-fg text-[10px] leading-none flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer"
                  title="Edit theme"
                ><Pencil className="lucide-inline" /></button>
              )}
            </div>
          ))}
          <button
            className={BTN + ' ' + (editor.editorOpen
              ? 'bg-accent-subtle text-accent border-accent'
              : 'border-dashed border-border-strong text-muted hover:text-accent hover:border-accent transition-colors'
            )}
            onClick={editor.editorOpen ? editor.closeEditor : editor.openNewTheme}
          >
            {editor.editorOpen && !editor.isEditing ? <><Pencil className="lucide-inline" /> Creating…</> : editor.editorOpen && editor.isEditing ? <><Pencil className="lucide-inline" /> Editing…</> : '+ New Theme'}
          </button>
        </div>

        {editor.editorOpen && (
          <div className="mt-4 border-t border-border pt-4 animate-rise">
            <ThemeEditorPanel editor={editor} />
          </div>
        )}
      </Card>
    </div>
  )
}
