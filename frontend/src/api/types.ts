// Everything the server sends. Ids are UUID strings, times ISO 8601 in UTC, durations seconds.

export type TaskStatus = "queued" | "running" | "done" | "failed" | "cancelled";
export type ActiveStatus = "queued" | "running";
export type FinishedStatus = "done" | "failed" | "cancelled";
export type WorkerState = "loading" | "idle" | "busy";

export interface SystemStatus {
  type: "system";
  running: number;
  waiting: number;
  new_image_ready_in_s: number;
}

/** A partial update of the active task; fields that weren't sent are absent. */
export interface TaskUpdate {
  type: "task";
  id: string;
  status: ActiveStatus;
  position?: number;
  queue_length?: number;
  progress?: number;
  stage?: string;
  ready_in_s?: number;
}

export interface TaskFinal {
  type: "task_final";
  id: string;
  status: FinishedStatus;
}

export interface TaskOut {
  task_id: string;
  status: TaskStatus;
  prompt: string;
  size: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  position: number | null;
  queue_length: number | null;
  progress: number | null;
  stage: string | null;
  ready_in_s: number | null;
  error_message: string | null;
}

export interface TaskPage {
  items: TaskOut[];
  next_before: string | null;
}

export interface Snapshot {
  type: "snapshot";
  system: SystemStatus;
  task: TaskOut | null;
}

export interface Ping {
  type: "ping";
}

export interface ApiStats {
  sockets: number;
  requests_per_min: number;
}

export interface MachineItem {
  name: string;
  role: string;
  cpu: number;
  ram: number;
  api: ApiStats | null;
}

export interface WorkerItem {
  name: string;
  machine: string;
  state: WorkerState;
  task_id: string | null;
  progress: number | null;
  rss_mb: number;
}

export interface TaskItem {
  id: string;
  user_email: string;
  size: number;
  status: ActiveStatus;
  worker_host: string | null;
  progress: number | null;
  position: number | null;
  seconds_so_far: number;
}

export interface Alert {
  at: string;
  message: string;
}

export interface AutoscalerItem {
  desired: number;
  ready: number;
  starting: number;
  maximum: number;
  last_action: string | null;
  alerts: Alert[];
}

export interface AdminOverview {
  type: "admin";
  machines: MachineItem[];
  workers: WorkerItem[];
  tasks: TaskItem[];
  autoscaler: AutoscalerItem;
}

export type ServerMessage = SystemStatus | TaskUpdate | TaskFinal | Snapshot | Ping | AdminOverview;

export interface User {
  email: string;
  role: string;
}

export interface CancelOut {
  status: TaskStatus;
}
