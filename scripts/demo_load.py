"""Several new users submit a job each at the same moment, so a queue with positions and ready times forms.

    python3 scripts/demo_load.py https://<domain> [--users 6] [--size 256] [--insecure] [--no-follow]

Prints each job's place in the queue and the ready time the server promised, then, unless --no-follow, one
line per job every few seconds until all have finished, with the actual time next to the promised one.
Ctrl-C stops following; the jobs keep running. It signs up throwaway users.
"""

import argparse
import sys
import time
import uuid

from smoke import FINISHED, Client, SmokeFailure, sign_up

PROMPTS = (
    "a lighthouse on a cliff at dusk",
    "a red bicycle leaning on a wall in the rain",
    "a bowl of ramen, studio lighting",
    "a fox in the snow, watercolour",
    "a sailing boat on a calm sea at dawn",
    "an old library with tall shelves",
)
FOLLOW_EVERY_S = 5


def minutes(seconds: float | None) -> str:
    return "-" if seconds is None else f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def submit_all(base_url: str, insecure: bool, users: int, size: int) -> list[dict]:
    # Every account first, so the submits land within a second of each other.
    clients = []
    for _ in range(users):
        client = Client(base_url, insecure)
        sign_up(client)
        clients.append(client)
    jobs = []
    for i, client in enumerate(clients):
        prompt = PROMPTS[i % len(PROMPTS)]
        answer = client.json(
            "POST",
            "/api/tasks",
            202,
            {"prompt": prompt, "size": size},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        jobs.append(
            {
                "client": client,
                "id": answer["task_id"],
                "prompt": prompt,
                "submitted": time.monotonic(),
                "promised": answer.get("ready_in_s"),
                "position": answer.get("position"),
                "status": answer["status"],
            }
        )
    print(f"{'user':>4}  {'task':36}  {'place':>5}  {'ready in':>8}  prompt")
    for i, job in enumerate(jobs, start=1):
        place = "-" if job["position"] is None else str(job["position"])
        print(f"{i:>4}  {job['id']:36}  {place:>5}  {minutes(job['promised']):>8}  {job['prompt']}")
    return jobs


def follow(jobs: list[dict]) -> None:
    while not all(job["status"] in FINISHED for job in jobs):
        time.sleep(FOLLOW_EVERY_S)
        line = []
        for i, job in enumerate(jobs, start=1):
            if job["status"] not in FINISHED:
                task = job["client"].json("GET", f"/api/tasks/{job['id']}", 200)
                job["status"] = task["status"]
                if task["status"] == "queued":
                    job["shown"] = f"#{task.get('position')} in {minutes(task.get('ready_in_s'))}"
                elif task["status"] == "running":
                    job["shown"] = f"{task.get('progress') or 0}%"
                else:
                    job["took"] = time.monotonic() - job["submitted"]
                    job["shown"] = task["status"]
            line.append(f"{i}: {job['shown']}")
        print(time.strftime("%H:%M:%S"), "  ".join(line))
    print(f"{'user':>4}  {'status':9}  {'promised':>8}  {'actual':>8}")
    for i, job in enumerate(jobs, start=1):
        print(f"{i:>4}  {job['status']:9}  {minutes(job['promised']):>8}  {minutes(job.get('took')):>8}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url", help="e.g. https://imagegen.example.com")
    parser.add_argument("--users", type=int, default=6, help="how many users submit (default 6)")
    parser.add_argument("--size", type=int, default=256, help="picture size in px (default 256)")
    parser.add_argument("--insecure", action="store_true", help="accept a self-signed certificate")
    parser.add_argument("--no-follow", action="store_true", help="exit once every job is submitted")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    try:
        jobs = submit_all(args.base_url, args.insecure, args.users, args.size)
        if not args.no_follow:
            follow(jobs)
    except SmokeFailure as exc:
        print(f"demo load FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped following; the jobs keep running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
