import { useState, type Dispatch, type FormEvent } from "react";
import { ApiError, imageUrl, submitTask } from "../api/http";
import type { SystemStatus, TaskOut } from "../api/types";
import { MAX_PROMPT_LEN, SIZES } from "../config";
import { errorText, failureSentence } from "../errors";
import type { Action, Draft } from "../state";
import { formatDuration } from "../time";

export function StatusLine({ system }: { system: SystemStatus }) {
  return (
    <p className="status">
      {system.running} generating · {system.waiting} waiting · {formatDuration(system.new_image_ready_in_s)}{" "}
      for a new image
    </p>
  );
}

function submitError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return "You already have an image in progress.";
    const retry = error.body?.retry_in_s;
    if (error.status === 503 && typeof retry === "number") {
      return `The queue is full right now. Try again in ${formatDuration(retry)}.`;
    }
  }
  return errorText(error);
}

export function TaskForm({ draft, dispatch }: { draft: Draft; dispatch: Dispatch<Action> }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft.prompt.trim()) {
      setError("Write what you want to see first.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      dispatch({ type: "submitted", task: await submitTask(draft.prompt, draft.size) });
    } catch (err) {
      setError(submitError(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} noValidate>
      <label htmlFor="prompt">Prompt</label>
      <textarea
        id="prompt"
        rows={4}
        maxLength={MAX_PROMPT_LEN}
        value={draft.prompt}
        onChange={(event) => dispatch({ type: "draft", draft: { prompt: event.target.value } })}
      />
      <label htmlFor="size">Size</label>
      <select
        id="size"
        value={draft.size}
        onChange={(event) => dispatch({ type: "draft", draft: { size: Number(event.target.value) } })}
      >
        {SIZES.map((size) => (
          <option key={size} value={size}>
            {size} px
          </option>
        ))}
      </select>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="primary" disabled={busy}>
        Generate
      </button>
    </form>
  );
}

type CancelHandler = (task: TaskOut) => Promise<string>;

function CancelLink({ task, onCancel }: { task: TaskOut; onCancel: CancelHandler }) {
  const [error, setError] = useState("");
  return (
    <>
      <button type="button" className="link" onClick={async () => setError(await onCancel(task))}>
        Cancel
      </button>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

function Queued({ task, onCancel }: { task: TaskOut; onCancel: CancelHandler }) {
  const parts = [
    task.position !== null && task.queue_length !== null
      ? `Position ${task.position} of ${task.queue_length}`
      : "Waiting in the queue",
  ];
  if (task.ready_in_s !== null) parts.push(`ready in ${formatDuration(task.ready_in_s)}`);
  return (
    <section className="job">
      <p className="queue-line">{parts.join(" · ")}</p>
      <CancelLink task={task} onCancel={onCancel} />
    </section>
  );
}

function Running({ task, onCancel }: { task: TaskOut; onCancel: CancelHandler }) {
  const progress = task.progress ?? 0;
  const detail = [task.stage, task.ready_in_s !== null && `${formatDuration(task.ready_in_s)} left`];
  return (
    <section className="job">
      <p className="percent">{progress}%</p>
      <div
        className="bar"
        role="progressbar"
        aria-label="Progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <div className="bar-fill" style={{ width: `${progress}%` }} />
      </div>
      <p className="detail">{detail.filter(Boolean).join(" · ")}</p>
      <CancelLink task={task} onCancel={onCancel} />
    </section>
  );
}

function Done({ task, dispatch }: { task: TaskOut; dispatch: Dispatch<Action> }) {
  return (
    <section className="job">
      <figure>
        <img className="result" src={imageUrl(task.task_id)} alt={task.prompt} />
        <figcaption>
          {task.prompt} · {task.size} px
        </figcaption>
      </figure>
      <button
        type="button"
        className="primary"
        onClick={() => dispatch({ type: "form", draft: { prompt: "", size: task.size } })}
      >
        Generate another
      </button>
    </section>
  );
}

function Failed({ task, dispatch }: { task: TaskOut; dispatch: Dispatch<Action> }) {
  return (
    <section className="job">
      <p>{failureSentence(task.error_message)}</p>
      <button
        type="button"
        className="primary"
        onClick={() => dispatch({ type: "form", draft: { prompt: task.prompt, size: task.size } })}
      >
        Try again
      </button>
    </section>
  );
}

export function TaskView({
  task,
  dispatch,
  onCancel,
}: {
  task: TaskOut;
  dispatch: Dispatch<Action>;
  onCancel: CancelHandler;
}) {
  switch (task.status) {
    case "queued":
      return <Queued task={task} onCancel={onCancel} />;
    case "running":
      return <Running task={task} onCancel={onCancel} />;
    case "done":
      return <Done task={task} dispatch={dispatch} />;
    case "failed":
      return <Failed task={task} dispatch={dispatch} />;
    case "cancelled":
      return null;
  }
}
