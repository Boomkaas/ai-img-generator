import type { Snapshot, SystemStatus, TaskFinal, TaskOut, TaskStatus, TaskUpdate } from "./api/types";
import { DEFAULT_SIZE } from "./config";

export interface Draft {
  prompt: string;
  size: number;
}

export interface State {
  system: SystemStatus | null;
  /** The task on screen: queued or running, or the finished one whose result is showing. */
  task: TaskOut | null;
  /** Ids that had task_final; a task message for one of them arrived late and is ignored. */
  finished: string[];
  draft: Draft;
}

export type Action =
  | Snapshot
  | SystemStatus
  | TaskUpdate
  | TaskFinal
  | { type: "restored"; task: TaskOut | null }
  | { type: "submitted"; task: TaskOut }
  | { type: "settled"; task: TaskOut }
  | { type: "cancelled"; id: string }
  | { type: "draft"; draft: Partial<Draft> }
  | { type: "form"; draft: Draft };

export const initialState: State = {
  system: null,
  task: null,
  finished: [],
  draft: { prompt: "", size: DEFAULT_SIZE },
};

// A task only moves forward: queued, running, then finished.
const RANK: Record<TaskStatus, number> = { queued: 0, running: 1, done: 2, failed: 2, cancelled: 2 };

export const isActive = (task: TaskOut | null) => task !== null && RANK[task.status] < 2;

function backToForm(state: State, task: TaskOut): State {
  return {
    ...state,
    task: null,
    finished: state.finished.includes(task.task_id) ? state.finished : [...state.finished, task.task_id],
    draft: { prompt: task.prompt, size: task.size },
  };
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "snapshot": {
      const task = action.task;
      if (task && !state.finished.includes(task.task_id)) return { ...state, system: action.system, task };
      // No active task while one is on screen as active: it ended while the socket was down, and the page
      // fetches it to settle. A finished result on screen stays.
      return { ...state, system: action.system };
    }
    case "system":
      return { ...state, system: action };
    case "task": {
      const current = state.task;
      if (!current || current.task_id !== action.id || state.finished.includes(action.id)) return state;
      if (RANK[action.status] < RANK[current.status]) return state;
      const running = action.status === "running";
      const task: TaskOut = {
        ...current,
        status: action.status,
        position: running ? null : (action.position ?? current.position),
        queue_length: running ? null : (action.queue_length ?? current.queue_length),
        progress: action.progress ?? current.progress,
        stage: action.stage ?? current.stage,
        ready_in_s: action.ready_in_s ?? current.ready_in_s,
      };
      return { ...state, task };
    }
    case "task_final": {
      const finished = state.finished.includes(action.id) ? state.finished : [...state.finished, action.id];
      if (action.status === "cancelled" && state.task?.task_id === action.id) {
        return backToForm(state, state.task);
      }
      // Done and failed wait for the full task, fetched by the page, before the view changes.
      return { ...state, finished };
    }
    case "restored":
      return action.task ? { ...state, task: action.task } : state;
    case "submitted":
      return { ...state, task: action.task };
    case "settled": {
      if (state.task?.task_id !== action.task.task_id) return state;
      if (action.task.status === "cancelled") return backToForm(state, action.task);
      if (RANK[action.task.status] < 2) return state;
      return { ...state, task: action.task };
    }
    case "cancelled":
      return state.task?.task_id === action.id ? backToForm(state, state.task) : state;
    case "draft":
      return { ...state, draft: { ...state.draft, ...action.draft } };
    case "form":
      return { ...state, task: null, draft: action.draft };
  }
}
