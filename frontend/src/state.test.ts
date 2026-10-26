import { describe, expect, it } from "vitest";
import type { SystemStatus } from "./api/types";
import { initialState, reducer, type State } from "./state";
import { task } from "./test/fetch";

const ID = task().task_id;
const OTHER = "22222222-2222-4222-8222-222222222222";
const system: SystemStatus = { type: "system", running: 3, waiting: 2, new_image_ready_in_s: 840 };

const withTask = (fields = {}): State => ({ ...initialState, task: task(fields) });

describe("reducer", () => {
  it("takes the system status and the active task from a snapshot", () => {
    const state = reducer(initialState, { type: "snapshot", system, task: task({ status: "running" }) });
    expect(state.system).toEqual(system);
    expect(state.task?.status).toBe("running");
  });

  it("keeps a finished result on screen when the snapshot has no active task", () => {
    const state = reducer(withTask({ status: "done" }), { type: "snapshot", system, task: null });
    expect(state.task?.status).toBe("done");
  });

  it("replaces the system status", () => {
    const state = reducer(withTask(), system);
    expect(state.system).toEqual(system);
    expect(state.task).toEqual(task());
  });

  it("merges a task message into the task with the same id", () => {
    const state = reducer(withTask({ status: "running", progress: 10 }), {
      type: "task",
      id: ID,
      status: "running",
      progress: 36,
      stage: "step 10 of 25",
    });
    expect(state.task).toMatchObject({ progress: 36, stage: "step 10 of 25", prompt: task().prompt });
  });

  it("clears the queue position once the task runs", () => {
    const state = reducer(withTask(), { type: "task", id: ID, status: "running", progress: 0 });
    expect(state.task).toMatchObject({ status: "running", position: null, queue_length: null });
  });

  it("ignores a task message for another id", () => {
    const before = withTask();
    expect(reducer(before, { type: "task", id: OTHER, status: "queued", position: 9 })).toBe(before);
  });

  it("ignores a late queued message for a task that is running", () => {
    const before = withTask({ status: "running", progress: 40 });
    expect(reducer(before, { type: "task", id: ID, status: "queued", position: 1 })).toBe(before);
  });

  it("ignores task messages after task_final", () => {
    const final = reducer(withTask({ status: "running" }), { type: "task_final", id: ID, status: "done" });
    expect(reducer(final, { type: "task", id: ID, status: "running", progress: 99 })).toBe(final);
  });

  it("goes back to the form with the prompt kept when task_final says cancelled", () => {
    const state = reducer(withTask({ size: 768 }), { type: "task_final", id: ID, status: "cancelled" });
    expect(state.task).toBeNull();
    expect(state.draft).toEqual({ prompt: task().prompt, size: 768 });
  });

  it("waits for the fetched task after task_final done, then shows it", () => {
    const final = reducer(withTask({ status: "running" }), { type: "task_final", id: ID, status: "done" });
    expect(final.task?.status).toBe("running");
    const settled = reducer(final, { type: "settled", task: task({ status: "done" }) });
    expect(settled.task?.status).toBe("done");
  });

  it("shows the failed task once it is fetched", () => {
    const final = reducer(withTask({ status: "running" }), { type: "task_final", id: ID, status: "failed" });
    const settled = reducer(final, {
      type: "settled",
      task: task({ status: "failed", error_message: "time limit exceeded" }),
    });
    expect(settled.task).toMatchObject({ status: "failed", error_message: "time limit exceeded" });
  });

  it("ignores a fetched task that is no longer on screen", () => {
    const before = { ...initialState };
    expect(reducer(before, { type: "settled", task: task({ status: "done" }) })).toBe(before);
  });
});
