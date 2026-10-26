import { describe, expect, it, vi } from "vitest";
import { json, stubFetch } from "../test/fetch";
import { ApiError, getActiveTask, getMe, NetworkError, onSignedOut, signIn } from "./http";

const user = { email: "ana@example.com", role: "user" };
const expired = () => json({ detail: "Please sign in again" }, 401);
const calls = (fetch: ReturnType<typeof stubFetch>, path: string) =>
  fetch.mock.calls.filter(([called]) => called === path).length;

function watchSignOut() {
  const listener = vi.fn();
  const stop = onSignedOut(listener);
  return { listener, stop };
}

describe("an expired access token", () => {
  it("is refreshed and the request retried", async () => {
    let refreshed = false;
    const fetch = stubFetch({
      "GET /api/auth/me": () => (refreshed ? json(user) : expired()),
      "POST /api/auth/refresh": () => {
        refreshed = true;
        return json(user);
      },
    });
    await expect(getMe()).resolves.toEqual(user);
    expect(calls(fetch, "/api/auth/refresh")).toBe(1);
    expect(calls(fetch, "/api/auth/me")).toBe(2);
  });

  it("is refreshed once for requests that fail together", async () => {
    let refreshed = false;
    let release = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    const fetch = stubFetch({
      "GET /api/auth/me": () => (refreshed ? json(user) : expired()),
      "GET /api/tasks/active": () => (refreshed ? json(null) : expired()),
      "POST /api/auth/refresh": async () => {
        await gate;
        refreshed = true;
        return json(user);
      },
    });
    const both = Promise.all([getMe(), getActiveTask()]);
    await new Promise((resolve) => setTimeout(resolve, 0));
    release();
    await expect(both).resolves.toEqual([user, null]);
    expect(calls(fetch, "/api/auth/refresh")).toBe(1);
  });

  it("signs the user out when the refresh is refused", async () => {
    stubFetch({ "GET /api/auth/me": expired, "POST /api/auth/refresh": expired });
    const { listener, stop } = watchSignOut();
    await expect(getMe()).rejects.toMatchObject({ status: 401 });
    expect(listener).toHaveBeenCalledOnce();
    stop();
  });

  it("doesn't sign the user out when the refresh gets no answer", async () => {
    stubFetch({
      "GET /api/auth/me": expired,
      "POST /api/auth/refresh": () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const { listener, stop } = watchSignOut();
    await expect(getMe()).rejects.toBeInstanceOf(NetworkError);
    expect(listener).not.toHaveBeenCalled();
    stop();
  });

  it("doesn't sign the user out when the refresh fails on the server", async () => {
    stubFetch({
      "GET /api/auth/me": expired,
      "POST /api/auth/refresh": () => json({ detail: "Something went wrong on our side." }, 500),
    });
    const { listener, stop } = watchSignOut();
    await expect(getMe()).rejects.toMatchObject({ status: 500 });
    expect(listener).not.toHaveBeenCalled();
    stop();
  });
});

it("takes a 401 from sign-in as the answer, without refreshing", async () => {
  const fetch = stubFetch({
    "POST /api/auth/login": () => json({ detail: "Email or password is incorrect" }, 401),
  });
  const error = await signIn("ana@example.com", "wrong").catch((err: unknown) => err);
  expect(error).toBeInstanceOf(ApiError);
  expect((error as ApiError).message).toBe("Email or password is incorrect");
  expect(calls(fetch, "/api/auth/refresh")).toBe(0);
});
