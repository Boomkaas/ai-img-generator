import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Meter } from "../admin/Meter";
import { Pill, type Tone } from "../admin/Pill";
import { cancelAnyTask, getAdminOverview } from "../api/http";
import { openSocket } from "../api/socket";
import type {
  AdminOverview,
  AutoscalerItem,
  MachineItem,
  TaskItem,
  WorkerItem,
  WorkerState,
} from "../api/types";
import { ADMIN_PAUSED_S } from "../config";
import { errorText } from "../errors";
import { formatClock, formatMinutes } from "../time";

const shortId = (id: string) => id.slice(0, 4);

export function AdminPage() {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    let stopped = false;
    let live = false;
    let pausedTimer: ReturnType<typeof setTimeout> | undefined;
    const expectOverview = () => {
      clearTimeout(pausedTimer);
      pausedTimer = setTimeout(() => setPaused(true), ADMIN_PAUSED_S * 1000);
    };

    expectOverview();
    // A failed first paint is left to the socket, and the paused line shows if that stays quiet too.
    getAdminOverview().then(
      (first) => {
        // An overview from the socket is newer than a first paint that answered late.
        if (!stopped && !live) setOverview(first);
      },
      () => undefined,
    );
    const close = openSocket((message) => {
      if (message.type !== "admin") return;
      live = true;
      setOverview(message);
      setPaused(false);
      expectOverview();
    });
    return () => {
      stopped = true;
      clearTimeout(pausedTimer);
      close();
    };
  }, []);

  return (
    <main className="wide">
      {paused && <p className="paused">Live data paused — reconnecting</p>}
      <header className="admin-head">
        <h1>Admin</h1>
        <Link to="/">Back to generator</Link>
      </header>
      {overview && (
        <>
          <Summary autoscaler={overview.autoscaler} />
          <Tasks tasks={overview.tasks} />
          <Workers workers={overview.workers} />
          <Machines machines={overview.machines} />
        </>
      )}
    </main>
  );
}

function Summary({ autoscaler }: { autoscaler: AutoscalerItem }) {
  const alerts = [...autoscaler.alerts].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  return (
    <section className="summary" aria-label="Autoscaler">
      <p className="summary-line">
        <span className="summary-label">Workers</span>
        <span>
          <span className="num">{autoscaler.ready}</span> ready
        </span>
        <span>
          <span className="num">{autoscaler.starting}</span> starting
        </span>
        <span>
          <span className="num">{autoscaler.maximum}</span> maximum
        </span>
        {autoscaler.last_action && <span className="dim">{autoscaler.last_action}</span>}
      </p>
      {alerts.length > 0 && (
        <ul className="alerts">
          {alerts.map((alert) => (
            <li key={`${alert.at} ${alert.message}`}>
              <time dateTime={alert.at}>{formatClock(alert.at)}</time> {alert.message}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Tasks({ tasks }: { tasks: TaskItem[] }) {
  const running = tasks.filter((task) => task.status === "running");
  const waiting = tasks
    .filter((task) => task.status === "queued")
    .sort((a, b) => (a.position ?? Infinity) - (b.position ?? Infinity));
  return (
    <section className="block" aria-labelledby="tasks-heading">
      <div className="block-head">
        <h2 id="tasks-heading">Tasks</h2>
        <span className="dim">
          {running.length} running · {waiting.length} waiting
        </span>
      </div>
      {tasks.length === 0 ? (
        <p className="empty">Nothing is running or waiting.</p>
      ) : (
        <div className="table-scroll">
          <table aria-labelledby="tasks-heading">
            <thead>
              <tr>
                <th>Task</th>
                <th>User</th>
                <th>Size</th>
                <th>Where</th>
                <th>Progress</th>
                <th>Time</th>
                <th>
                  <span className="visually-hidden">Cancel</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {[...running, ...waiting].map((task) => (
                <TaskRow key={task.id} task={task} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function TaskRow({ task }: { task: TaskItem }) {
  const [step, setStep] = useState<"idle" | "asking" | "sending" | "sent">("idle");
  const [error, setError] = useState("");
  const cancelButton = useRef<HTMLButtonElement>(null);
  const noButton = useRef<HTMLButtonElement>(null);
  const backFromAsking = useRef(false);

  // The button that was clicked disappears, so focus moves to the one that replaces it.
  useEffect(() => {
    if (step === "asking") noButton.current?.focus();
    if (step === "idle" && backFromAsking.current) {
      backFromAsking.current = false;
      cancelButton.current?.focus();
    }
  }, [step]);

  async function cancel() {
    setStep("sending");
    setError("");
    try {
      await cancelAnyTask(task.id);
      setStep("sent");
    } catch (failure) {
      setError(errorText(failure));
      setStep("idle");
    }
  }

  const running = task.status === "running";
  return (
    <tr>
      <td className="mono">{shortId(task.id)}</td>
      <td>{task.user_email}</td>
      <td>{task.size} px</td>
      <td>
        {running ? (
          <span className="mono">{task.worker_host ?? "—"}</span>
        ) : (
          <Pill>#{task.position} in queue</Pill>
        )}
      </td>
      <td>{running && task.progress !== null ? <Meter percent={task.progress} /> : "—"}</td>
      <td>
        {formatMinutes(task.seconds_so_far)} {running ? "running" : "waiting"}
      </td>
      <td>
        {step === "idle" && (
          <>
            <button ref={cancelButton} type="button" className="link" onClick={() => setStep("asking")}>
              Cancel
            </button>
            {error && (
              <span className="cell-error" role="alert">
                {error}
              </span>
            )}
          </>
        )}
        {(step === "asking" || step === "sending") && (
          <span className="ask">
            Cancel this task?{" "}
            <button type="button" className="link" onClick={cancel} disabled={step === "sending"}>
              Yes
            </button>{" "}
            <button
              ref={noButton}
              type="button"
              className="link"
              disabled={step === "sending"}
              onClick={() => {
                backFromAsking.current = true;
                setStep("idle");
              }}
            >
              No
            </button>
          </span>
        )}
        {step === "sent" && <span className="dim">Cancelling…</span>}
      </td>
    </tr>
  );
}

const STATE_TONE: Record<WorkerState, Tone> = { busy: "accent", idle: "dim", loading: "warn" };

function Workers({ workers }: { workers: WorkerItem[] }) {
  return (
    <section className="block" aria-labelledby="workers-heading">
      <div className="block-head">
        <h2 id="workers-heading">Workers</h2>
        <span className="dim">one job at a time each</span>
      </div>
      {workers.length === 0 ? (
        <p className="empty">No worker is running.</p>
      ) : (
        <div className="table-scroll">
          <table aria-labelledby="workers-heading">
            <thead>
              <tr>
                <th>Worker</th>
                <th>State</th>
                <th>Task</th>
                <th>Progress</th>
                <th>Memory</th>
              </tr>
            </thead>
            <tbody>
              {workers.map((worker) => (
                <tr key={worker.name}>
                  <td className="mono">{worker.name}</td>
                  <td>
                    <Pill tone={STATE_TONE[worker.state]}>{worker.state}</Pill>
                  </td>
                  <td className={worker.task_id ? "mono" : undefined}>
                    {worker.task_id ? shortId(worker.task_id) : "—"}
                  </td>
                  <td>{worker.progress !== null ? <Meter percent={worker.progress} /> : "—"}</td>
                  <td>{(worker.rss_mb / 1024).toFixed(1)} GB</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Machines({ machines }: { machines: MachineItem[] }) {
  return (
    <section className="block" aria-labelledby="machines-heading">
      <div className="block-head">
        <h2 id="machines-heading">Machines</h2>
      </div>
      <div className="table-scroll">
        <table aria-labelledby="machines-heading">
          <thead>
            <tr>
              <th>Machine</th>
              <th>Role</th>
              <th>CPU</th>
              <th>RAM</th>
              <th>API server</th>
            </tr>
          </thead>
          <tbody>
            {machines.map((machine) => (
              <tr key={machine.name}>
                <td className="mono">{machine.name}</td>
                <td>{machine.role}</td>
                <td>
                  <Meter percent={machine.cpu} load />
                </td>
                <td>
                  <Meter percent={machine.ram} load />
                </td>
                <td>{machine.api ? apiLine(machine.api.sockets, machine.api.requests_per_min) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const apiLine = (sockets: number, perMinute: number) =>
  `${sockets} ${sockets === 1 ? "socket" : "sockets"} · ${perMinute} req/min`;
