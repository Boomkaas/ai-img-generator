import { ApiError, NetworkError } from "./api/http";

export const CANT_REACH = "Can't reach the server. Check your connection and try again.";

export function errorText(error: unknown): string {
  if (error instanceof NetworkError) return CANT_REACH;
  if (error instanceof ApiError && typeof error.body?.detail === "string") return error.body.detail;
  return "Something went wrong. Please try again.";
}

export function failureSentence(errorMessage: string | null): string {
  switch (errorMessage) {
    case "time limit exceeded":
      return "This image took longer than the time limit allows.";
    case "the worker stopped unexpectedly":
      return "This image couldn't be finished because its worker stopped.";
    default:
      return "This image couldn't be finished.";
  }
}
