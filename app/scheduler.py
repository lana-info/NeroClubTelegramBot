from __future__ import annotations

import asyncio
import os
import time

import httpx


JOBS = {
    "/internal/jobs/process-stripe": 3600,
    "/internal/jobs/process-site-access": 3600,
    "/internal/jobs/send-reminders": 86400,
    "/internal/jobs/reconcile-telegram": 86400,
    "/internal/jobs/process-telegram-restores": 3600,
}


async def run_job(client: httpx.AsyncClient, base_url: str, token: str, path: str) -> int:
    response = await client.post(
        base_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    return response.status_code


async def main() -> None:
    base_url = os.getenv("BACKEND_INTERNAL_URL", "http://bot-backend:8000")
    token = os.getenv("ADMIN_API_TOKEN", "")
    if not token:
        print("scheduler waiting for ADMIN_API_TOKEN", flush=True)
        while True:
            await asyncio.sleep(3600)
    interval = max(10, int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60")))
    last_run: dict[str, float] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            now = time.monotonic()
            for path, period in JOBS.items():
                if now - last_run.get(path, 0) < period:
                    continue
                try:
                    status = await run_job(client, base_url, token, path)
                    print(f"scheduler job={path} status={status}", flush=True)
                except httpx.HTTPError as exc:
                    print(f"scheduler job={path} error={type(exc).__name__}", flush=True)
                last_run[path] = now
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
