import { useState, useEffect } from 'react';
import { Wrench, Shield, Save, Check, X as XIcon } from 'lucide-react';
import { SendBtn, Badge } from '../../components/ui';
import DetailPanel from '../../components/DetailPanel';
import type { TaskDetail } from '../../types';

import { i18nT } from '../../i18n/t'
import { fmtDateFields, fmtDuration as fmtDurationParts, fmtUnit } from '../../i18n/format'
interface Props {
  task: TaskDetail;
  allTasks?: TaskDetail[];
  onClose: () => void;
  onRetry?: (index: number) => void;
  onApprove?: (decision: 'approve' | 'reject') => void;
  onToggleApproval?: (index: number, field: 'requires_approval' | 'force_approval', value: boolean) => Promise<boolean>;
  editable?: boolean;
  onSave?: (index: number, updates: { title: string; description: string; depends_on: number[] }) => Promise<{ title: string; description: string; depends_on: number[] } | void>;
  pendingEdits?: Record<number, { title: string; description: string; depends_on: number[] }>;
  onEdit?: (index: number, updates?: { title: string; description: string; depends_on: number[] }) => void;
}

function fmtTime(ts?: number) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return fmtDateFields(d, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDuration(start?: number, end?: number): string {
  if (!start) return '—';
  const secs = Math.floor(((end || Date.now() / 1000) - start));
  if (secs < 60) return fmtUnit(secs, 'second', { maximumFractionDigits: 0 });
  const m = Math.floor(secs / 60), s = secs % 60;
  return m < 60
    ? fmtDurationParts([[m, 'minute'], [s, 'second']])
    : fmtDurationParts([[Math.floor(m / 60), 'hour'], [m % 60, 'minute']]);
}

export default function TaskDetailPanel({ task, allTasks = [], onClose, onRetry, onApprove, onToggleApproval, editable, onSave, pendingEdits = {}, onEdit }: Props) {
  const typeIcon = task.task_type === 'fix' ? <Wrench className="lucide-inline" /> : task.task_type === 'checkpoint' ? <Shield className="lucide-inline" /> : null;
  const pending = pendingEdits[task.index];

  const [editTitle, setEditTitle] = useState(pending?.title ?? task.title);
  const [editDesc, setEditDesc] = useState(pending?.description ?? task.description);
  const [editDeps, setEditDeps] = useState<number[]>(pending?.depends_on ?? task.depends_on ?? []);
  const [dirty, setDirty] = useState(!!pending);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    const p = pendingEdits[task.index];
    setEditTitle(p?.title ?? task.title);
    setEditDesc(p?.description ?? task.description);
    setEditDeps(p?.depends_on ?? task.depends_on ?? []);
    setDirty(!!p);
    // Only reset form state on task switch; closure captures latest values.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.index]);

  const reportEdit = (title: string, desc: string, deps: number[]) => {
    const origDeps = task.depends_on ?? [];
    const same = title === task.title && desc === task.description
      && deps.length === origDeps.length && deps.every(d => origDeps.includes(d));
    if (same) {
      onEdit?.(task.index, undefined);
      setDirty(false);
    } else {
      onEdit?.(task.index, { title, description: desc, depends_on: deps });
      setDirty(true);
    }
  };

  const handleSave = async () => {
    if (saving || !onSave) return;
    setSaving(true);
    setSaveError(null);
    try {
      const result = await onSave(task.index, { title: editTitle, description: editDesc, depends_on: editDeps });
      if (result) { setEditTitle(result.title); setEditDesc(result.description); setEditDeps(result.depends_on); }
      setDirty(false);
    } catch { setSaveError(i18nT('pages.aidlc.taskDetailPanel.save_failed_try_again')); } finally { setSaving(false); }
  };

  const toggleDep = (idx: number) => {
    const next = editDeps.includes(idx) ? editDeps.filter(d => d !== idx) : [...editDeps, idx];
    setEditDeps(next);
    reportEdit(editTitle, editDesc, next);
  };

  const hasFooterContent = (editable && dirty) || (editable && saveError) || (task.status === 'failed' && onRetry);
  const footer = hasFooterContent ? (
    <>
      <div>{saveError && <span className="text-[12px] text-danger">{saveError}</span>}</div>
      <div className="flex gap-2">
        {editable && dirty && <SendBtn onClick={handleSave} disabled={saving}><Save className="lucide-inline" /> {i18nT('pages.aidlc.taskDetailPanel.save')}</SendBtn>}
        {task.status === 'failed' && onRetry && <SendBtn onClick={() => onRetry(task.index)}>{i18nT('pages.aidlc.taskDetailPanel.retry_task')}</SendBtn>}
      </div>
    </>
  ) : undefined;

  return (
    <DetailPanel
      title={editable
        ? <>{typeIcon} {i18nT('pages.aidlc.taskDetailPanel.task')} {task.index}: <input aria-label={i18nT('pages.aidlc.taskDetailPanel.task_title')} value={editTitle} disabled={saving} onChange={e => { setEditTitle(e.target.value); reportEdit(e.target.value, editDesc, editDeps) }}
            className="bg-transparent border-b border-accent text-text text-[14px] outline-none w-[200px]" /></>
        : <>{typeIcon} {i18nT('pages.aidlc.taskDetailPanel.task')} {task.index}: {task.title}</>}
      onClose={onClose}
      initialWidth={420}
      footer={footer}
    >
        {/* Status */}
        <div className="flex items-center gap-2">
          <Badge variant={task.status === 'passed' || task.status === 'done' ? 'ok' : task.status === 'failed' ? 'err' : task.status === 'in_progress' ? 'aim' : 'warn'}>
            {task.status.replace('_', ' ')}
          </Badge>
          <span className="text-[12px] text-muted">{i18nT('pages.aidlc.taskDetailPanel.attempts')} {task.attempts}/3</span>
        </div>

        {/* Timestamps */}
        <div className="grid grid-cols-2 gap-2 text-[12px] text-muted">
          <div>
            <div className="font-semibold mb-0.5">{i18nT('pages.aidlc.taskDetailPanel.created')}</div>
            <div>{fmtTime(task.created_at || task.started_at)}</div>
          </div>
          <div>
            <div className="font-semibold mb-0.5">{i18nT('pages.aidlc.taskDetailPanel.duration')}</div>
            <div>{fmtDuration(task.started_at, task.finished_at)}</div>
          </div>
          {task.started_at && <div>
            <div className="font-semibold mb-0.5">{i18nT('pages.aidlc.taskDetailPanel.started')}</div>
            <div>{fmtTime(task.started_at)}</div>
          </div>}
          {task.finished_at && <div>
            <div className="font-semibold mb-0.5">{i18nT('pages.aidlc.taskDetailPanel.finished')}</div>
            <div>{fmtTime(task.finished_at)}</div>
          </div>}
        </div>

        {/* Description */}
        {editable ? (
          <textarea aria-label={i18nT('pages.aidlc.taskDetailPanel.task_description')} value={editDesc} disabled={saving} onChange={e => { setEditDesc(e.target.value); reportEdit(editTitle, e.target.value, editDeps) }}
            className="w-full text-[13px] leading-relaxed bg-bg-elevated border border-border rounded-md p-2 text-text outline-none resize-y min-h-[80px]" />
        ) : (
          <div className="text-[13px] leading-relaxed whitespace-pre-wrap">{task.description}</div>
        )}

        {task.error && (
          <div className="p-2.5 bg-danger/10 border border-danger/20 rounded-md text-[12px] text-danger">{task.error}</div>
        )}
        {task.result && (
          <div className="p-2.5 bg-bg-elevated border rounded-md text-[12px] text-text whitespace-pre-wrap max-h-[300px] overflow-auto" style={{ borderColor: 'color-mix(in srgb, var(--ok) 45%, transparent)' }}>{task.result}</div>
        )}

        {/* Dependencies */}
        {editable ? (
          <div>
            <div className="text-[12px] font-semibold text-muted mb-1">{i18nT('pages.aidlc.taskDetailPanel.depends_on')}</div>
            <div className="flex flex-col gap-1">
              {allTasks.filter(at => at.index < task.index).map(at => (
                <label key={at.index} htmlFor={`dep-${at.index}`} className="flex items-center gap-2 text-[12px] text-text cursor-pointer">
                  <input id={`dep-${at.index}`} type="checkbox" aria-label={i18nT('pages.aidlc.taskDetailPanel.depend_on_task', { n: at.index, title: at.title })} checked={editDeps.includes(at.index)} disabled={saving} onChange={() => toggleDep(at.index)} />
                  {i18nT('pages.aidlc.taskDetailPanel.task')} {at.index}: {at.title}
                </label>
              ))}
            </div>
          </div>
        ) : (task.depends_on || []).length > 0 && (() => {
          const deps = (task.depends_on || []).map(d => allTasks.find(t => t.index === d)).filter(Boolean) as TaskDetail[]
          const blocking = deps.filter(d => d.status !== 'passed' && d.status !== 'done')
          const isBlocked = task.status === 'pending' && blocking.length > 0
          return isBlocked ? (
            <div className="p-2.5 bg-bg-elevated border rounded-md text-[12px] text-text" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
              {i18nT('pages.aidlc.taskDetailPanel.blocked_waiting_on')} {i18nT('pages.aidlc.taskDetailPanel.task_2', { count: blocking.length })}:
              {blocking.map(d => (
                <div key={d.index} className="mt-1 pl-4">
                  {i18nT('pages.aidlc.taskDetailPanel.task')} {d.index}: {d.title} — <span className={d.status === 'failed' ? 'text-danger' : d.status === 'in_progress' ? 'text-accent' : 'text-muted'}>{d.status.replace('_', ' ')}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[12px] text-muted">
              {i18nT('pages.aidlc.taskDetailPanel.depends_on_2')} {deps.map(d => i18nT('pages.aidlc.taskDetailPanel.task_with_title', { n: d.index, title: d.title })).join(', ')}
            </div>
          )
        })()}

        {editable && onToggleApproval && (
          <ApprovalToggles task={task} onToggleApproval={onToggleApproval} />
        )}

        {(task.force_approval || task.requires_approval) && task.status === 'in_progress' && onApprove && (
          <div className="p-2.5 bg-bg-elevated border rounded-md text-[12px]" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
            <div className="text-text mb-2">{task.force_approval ? i18nT('pages.aidlc.taskDetailPanel.this_gate_blocks_force_approval_true') : i18nT('pages.aidlc.taskDetailPanel.requires_approval_before_execution_2')}</div>
            <div className="flex gap-2">
              <button className="px-3 py-1.5 rounded-md bg-ok text-white text-[12px] font-semibold cursor-pointer border-none hover:opacity-80 transition-all" onClick={() => onApprove('approve')}><Check className="lucide-inline" /> {i18nT('pages.aidlc.taskDetailPanel.approve')}</button>
              <button className="px-3 py-1.5 rounded-md bg-danger text-white text-[12px] font-semibold cursor-pointer border-none hover:opacity-80 transition-all" onClick={() => onApprove('reject')}><XIcon className="lucide-inline" /> {i18nT('pages.aidlc.taskDetailPanel.deny')}</button>
            </div>
          </div>
        )}
        {(task.force_approval || task.requires_approval) && task.status === 'pending' && (
          <div className="p-2.5 bg-bg-elevated border rounded-md text-[12px] text-text" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
            {i18nT('pages.aidlc.taskDetailPanel.requires_approval_before_execution')}
          </div>
        )}
      </DetailPanel>
  );
}

function ApprovalToggles({ task, onToggleApproval }: { task: TaskDetail; onToggleApproval: (index: number, field: 'requires_approval' | 'force_approval', value: boolean) => Promise<boolean> }) {
  const [ra, setRA] = useState(task.requires_approval || false);
  const [fa, setFA] = useState(task.force_approval || false);
  useEffect(() => { setRA(task.requires_approval || false); setFA(task.force_approval || false); }, [task.index, task.requires_approval, task.force_approval]);
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor="requires-approval" className="flex items-center gap-2 text-[12px] text-text cursor-pointer">
        <input id="requires-approval" type="checkbox" aria-label={i18nT('pages.aidlc.taskDetailPanel.requires_approval')} checked={ra} onChange={async e => { const v = e.target.checked; const prevRA = ra; const prevFA = fa; setRA(v); if (!v && fa) setFA(false); const ok = await onToggleApproval(task.index, 'requires_approval', v); if (!ok) { setRA(prevRA); setFA(prevFA); } }} />
        {i18nT('pages.aidlc.taskDetailPanel.requires_approval')}
      </label>
      {(ra || fa) && (
        <label htmlFor="force-approval" className="flex items-center gap-2 text-[12px] text-text cursor-pointer pl-4">
          <input id="force-approval" type="checkbox" aria-label={i18nT('pages.aidlc.taskDetailPanel.block_in_yolo_mode')} checked={fa} onChange={async e => { const v = e.target.checked; const prev = fa; setFA(v); const ok = await onToggleApproval(task.index, 'force_approval', v); if (!ok) setFA(prev); }} />
          {i18nT('pages.aidlc.taskDetailPanel.block_in_yolo_mode')}
        </label>
      )}
    </div>
  );
}
