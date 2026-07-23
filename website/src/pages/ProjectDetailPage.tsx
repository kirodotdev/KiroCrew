import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { AnimatePresence, motion } from 'framer-motion';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAppDispatch } from '../store';
import { setPendingInput } from '../store/chatSlice';
import type { ProjectRun, RunStatus } from '../types';
import DagView from './aidlc/DagView';
import PhasedView from './aidlc/PhasedView';
import TaskDetailPanel from './aidlc/TaskDetailPanel';
import PixelCanvasWidget from '../components/PixelCanvasWidget';
import { api } from '../api/client';
import { AlertTriangle, Download } from 'lucide-react';

type Tab = 'idea' | 'tasks';
type ViewMode = 'dag' | 'phased';

/** Minimal shape of a step returned by api.updatePlan (subset of TaskDetail). */
interface SavedStep {
  index: number;
  title: string;
  description: string;
  depends_on: number[];
}

interface Props {
  run: ProjectRun;
  onRetry?: (index: number) => void;
  onRefresh?: () => void;
}

const tabCls = (active: boolean) =>
  `px-4 py-1.5 text-[13px] rounded cursor-pointer border transition-all ${active ? 'bg-accent text-accent-fg border-accent' : 'bg-transparent text-muted border-border hover:text-text hover:border-border-strong'}`;

export default function ProjectDetailPage({ run, onRetry, onRefresh }: Props) {
  const [tab, setTab] = useState<Tab>('tasks');
  const [view, setView] = useState<ViewMode>('dag');
  const [selectedTask, setSelectedTask] = useState<number | null>(null);
  const [pendingEdits, setPendingEdits] = useState<Record<number, { title: string; description: string; depends_on: number[] }>>({});
  const [savedOverrides, setSavedOverrides] = useState<Record<number, { title: string; description: string; depends_on: number[] }>>({});
  useEffect(() => { setPendingEdits({}); setSavedOverrides({}); setSelectedTask(null); }, [run.task_id]);
  const dispatch = useAppDispatch();
  const navigate = useNavigate();

  const tasks = useMemo(() => (run.task_details || []).map(t => savedOverrides[t.index] ? { ...t, ...savedOverrides[t.index] } : t), [run.task_details, savedOverrides]);
  const idea = run.spec_content || run.original_input || '';
  const selected = selectedTask !== null ? tasks.find(t => t.index === selectedTask) : null;

  // Poll pending approvals for force_approval gates
  const { data: approvalMap = {} } = useQuery({
    queryKey: ['approvals', run.task_id],
    queryFn: async () => {
      const list = await api.approvals();
      const map: Record<number, string> = {};
      for (const a of list) {
        const m = a.id?.match(/^task-gate-(\d+)-/);
        if (m) map[Number(m[1])] = a.id;
      }
      return map;
    },
    enabled: run.status === 'running',
    refetchInterval: 3000,
  });

  const queryClient = useQueryClient();
  const { mutate: handleApprove } = useMutation({
    mutationFn: async (decision: 'approve' | 'reject') => {
      if (!selected || !approvalMap[selected.index]) return;
      return api.resolveApproval(approvalMap[selected.index], decision);
    },
    onSuccess: (_, decision) => {
      queryClient.invalidateQueries({ queryKey: ['approvals', run.task_id] });
      onRefresh?.();
      if (decision === 'reject' && selected) setSelectedTask(selected.index);
    },
  });
  const { mutate: dagApprove } = useMutation({
    mutationFn: async ({ index, decision }: { index: number; decision: 'approve' | 'reject' }) => {
      const approvalId = approvalMap[index];
      if (!approvalId) return;
      return api.resolveApproval(approvalId, decision);
    },
    onSuccess: (_, { index, decision }) => {
      queryClient.invalidateQueries({ queryKey: ['approvals', run.task_id] });
      onRefresh?.();
      if (decision === 'reject') setSelectedTask(index);
    },
  });
  const { mutateAsync: toggleApprovalMut } = useMutation({
    mutationFn: ({ index, updates }: { index: number; updates: Record<string, boolean> }) =>
      api.updateTask(run.task_id, index, updates),
    onSuccess: () => { onRefresh?.(); queryClient.invalidateQueries({ queryKey: ['approvals', run.task_id] }); },
  });
  const exportMutation = useMutation({
    mutationFn: () => api.exportPlanYaml(run.task_id),
    onError: (e) => {
      // eslint-disable-next-line no-console -- surface plan-export failures for debugging
      console.error('Failed to export plan YAML:', e);
    },
  });
  const handleToggleApproval = useCallback(async (index: number, field: 'requires_approval' | 'force_approval', value: boolean): Promise<boolean> => {
    try {
      const updates: Record<string, boolean> = { [field]: value };
      if (field === 'requires_approval' && !value) updates.force_approval = false;
      const res = await toggleApprovalMut({ index, updates });
      return !!res?.ok || !('ok' in (res || {}));
    } catch { return false; }
  }, [toggleApprovalMut]);
  const isPlanning = run.status === 'planning';
  const editableStatuses: RunStatus[] = ['planned', 'failed', 'cancelled', 'running', 'paused'];
  const editable = editableStatuses.includes(run.status);
  const pendingEditIds = useMemo(() => new Set(Object.keys(pendingEdits)), [pendingEdits]);
  const pendingEditIndexes = useMemo(() => new Set(Object.keys(pendingEdits).map(Number)), [pendingEdits]);

  const handleEdit = useCallback((index: number, updates?: { title: string; description: string; depends_on: number[] }) => {
    setPendingEdits(prev => {
      if (!updates) { const { [index]: _, ...rest } = prev; return rest; }
      return { ...prev, [index]: updates };
    });
  }, []);

  const savedOverridesRef = useRef(savedOverrides);
  savedOverridesRef.current = savedOverrides;

  const { mutateAsync: updateSingleTask } = useMutation({
    mutationFn: ({ index, updates }: { index: number; updates: { title: string; description: string; depends_on: number[] } }) =>
      api.updateTask(run.task_id, index, updates),
    onSuccess: (res, { index }) => {
      const override = { title: res.title, description: res.description, depends_on: res.depends_on };
      setSavedOverrides(prev => ({ ...prev, [index]: override }));
      setPendingEdits(prev => { const next = { ...prev }; delete next[index]; return next; });
    },
  });
  const handleSaveTask = useCallback(async (index: number, updates: { title: string; description: string; depends_on: number[] }) => {
    // For active runs (running/paused), use single-task update endpoint
    if (run.status === 'running' || run.status === 'paused') {
      const res = await updateSingleTask({ index, updates });
      return { title: res.title, description: res.description, depends_on: res.depends_on };
    }
    // For stopped runs (planned/failed/cancelled), use full plan update
    const currentOverrides = savedOverridesRef.current;
    const updatedSteps = (run.task_details || []).map(t =>
      currentOverrides[t.index] ? { ...t, ...currentOverrides[t.index] } : t
    ).map(t => t.index === index ? { ...t, ...updates } : t);
    try {
      const res = await api.updatePlan(run.task_id, updatedSteps.map(t => ({
        title: t.title, description: t.description, depends_on: t.depends_on, requires_approval: t.requires_approval,
      })));
      const savedSteps: SavedStep[] = res.steps || [];
      const saved = savedSteps.find((s) => s.index === index);
      const override = saved ? { title: saved.title, description: saved.description, depends_on: saved.depends_on } : updates;
      setSavedOverrides(prev => ({ ...prev, [index]: override }));
      setPendingEdits(prev => { const next = { ...prev }; delete next[index]; return next; });
      return override;
    } catch (e) {
      // eslint-disable-next-line no-console -- surface plan-update failures for debugging
      console.error('Failed to update plan:', e);
      throw e;
    }
  }, [run.task_details, run.task_id, run.status, updateSingleTask]);

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden">
      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        {/* Tab bar */}
        <div className="px-4 py-2 border-b border-border flex gap-1 items-center shrink-0">
          <button onClick={() => setTab('idea')} className={tabCls(tab === 'idea')}>Idea</button>
          <button onClick={() => setTab('tasks')} className={tabCls(tab === 'tasks')}>Tasks</button>
          {tab === 'tasks' && !isPlanning && (
            <>
              <span className="mx-1 text-muted">·</span>
              <button onClick={() => setView('dag')} className={tabCls(view === 'dag')}>DAG</button>
              <button onClick={() => setView('phased')} className={tabCls(view === 'phased')}>Phased</button>
            </>
          )}
          <div className="flex-1" />
          {!isPlanning && (run.task_details || []).length > 0 && (
            <button
              onClick={() => exportMutation.mutate()}
              disabled={exportMutation.isPending}
              title="Export this plan as a YAML workflow — re-importable via the 'From YAML' tab"
              className={`flex items-center gap-1.5 px-3 py-1.5 text-[13px] rounded border border-border text-muted cursor-pointer transition-all hover:text-accent hover:border-accent ${exportMutation.isPending ? 'opacity-50 cursor-not-allowed' : ''}`}
            >
              <Download size={13} /> {exportMutation.isPending ? 'Exporting…' : 'Export YAML'}
            </button>
          )}
          {!isPlanning && <PixelCanvasWidget run={run} />}
        </div>

        {/* Approval banner */}
        {run.status === 'running' && Object.keys(approvalMap).length > 0 && (() => {
          const tasks = run.task_details || [];
          const pendingTasks = Object.keys(approvalMap).map(Number).map(idx => tasks.find(t => t.index === idx)).filter(t => t && t.status === 'in_progress');
          if (!pendingTasks.length) return null;
          const t = pendingTasks[0]!;
          return (
            <div className="mx-4 mt-2 px-4 py-2.5 bg-[#eab308]/10 border border-[#eab308]/40 rounded-md flex items-center gap-3 text-[13px] shrink-0">
              <span className="text-[#eab308]"><AlertTriangle size={16} /></span>
              <span className="text-[#eab308]/90 flex-1"><strong>Approval required:</strong> Task {t.index} "{t.title}" is waiting for your decision</span>
              <button onClick={() => setSelectedTask(t.index)} className="px-3 py-1 bg-[#eab308] hover:bg-[#ca8a04] text-black text-[13px] rounded font-medium cursor-pointer border-none transition-all">Go to Task →</button>
            </div>
          );
        })()}
        {/* Content */}
        <div className="flex-1 min-h-0 min-w-0 overflow-auto p-4">
          {isPlanning ? (
            <PlanningOverlay />
          ) : tab === 'idea' ? (
            idea ? (
              <div>
                <div className="whitespace-pre-wrap text-[13px] text-muted bg-bg-elevated rounded-lg p-4 max-h-[500px] overflow-auto border border-border">{idea}</div>
                <button onClick={() => { dispatch(setPendingInput(idea)); navigate('/chat?prefill=plan') }}
                  className="mt-3 px-4 py-1.5 text-[13px] rounded-md bg-accent text-accent-fg border-none cursor-pointer hover:bg-accent-hover transition-all">
                  Edit in Chat
                </button>
              </div>
            ) : (
              <div className="text-muted text-[13px]">No idea or spec content available.</div>
            )
          ) : view === 'dag' ? (
            <DagView
              nodes={tasks.map(t => ({ id: String(t.index), title: t.title, status: t.status, task_type: t.task_type, requires_approval: t.requires_approval }))}
              edges={tasks.flatMap(t => (t.depends_on || []).map((d: number) => ({ from: String(d), to: String(t.index) })))}
              onNodeClick={(id) => setSelectedTask(Number(id))}
              selectedId={selectedTask !== null ? String(selectedTask) : undefined}
              pendingEditIds={pendingEditIds}
              approvalMap={run.status === 'running' ? approvalMap : undefined}
              onApprove={(index, decision) => dagApprove({ index, decision })}
            />
          ) : (
            <PhasedView tasks={tasks} onTaskClick={setSelectedTask} selectedIndex={selectedTask} pendingEditIndexes={pendingEditIndexes} />
          )}
        </div>
      </div>

      <AnimatePresence>
        {selected && (
          <motion.div
            key="task-panel"
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 'auto', opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="shrink-0 overflow-hidden h-full"
          >
            <TaskDetailPanel task={selected} allTasks={tasks} onClose={() => setSelectedTask(null)} onRetry={onRetry} onApprove={approvalMap[selected.index] ? handleApprove : undefined} onToggleApproval={editable ? handleToggleApproval : undefined} editable={editable && ((run.status === 'running' || run.status === 'paused') ? selected.status === 'pending' : true)} onSave={handleSaveTask} pendingEdits={pendingEdits} onEdit={handleEdit} />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function PlanningOverlay() {
  const [dots, setDots] = useState('');
  useEffect(() => {
    const iv = setInterval(() => setDots(d => d.length >= 3 ? '' : d + '.'), 500);
    return () => clearInterval(iv);
  }, []);
  return (
    <div className="flex items-center justify-center h-full">
      <div className="text-center">
        <div className="w-10 h-10 border-3 border-accent/30 border-t-accent rounded-full animate-spin mx-auto mb-4" />
        <div className="text-accent text-[16px] font-semibold">Generating execution plan{dots}</div>
        <div className="text-muted text-[13px] mt-1">Analyzing task and building step-by-step plan</div>
        <div className="text-muted text-[12px] mt-3">The DAG view will appear once the plan is ready</div>
      </div>
    </div>
  );
}
