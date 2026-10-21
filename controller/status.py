"""The live status every page shows, recomputed and published once per tick."""

from common import db, estimate, live, overview
from common.messages import SystemStatus, TaskUpdate


def publish_status() -> None:
    """The system status to everyone, each active task's place and ready time to its owner, the overview to
    admins.

    The counts come from Postgres, never from a counter in Redis, so a crashed worker can't leave them wrong.
    """
    spu = estimate.seconds_per_cost_unit()
    active = db.active_tasks()
    forecast = estimate.forecast(active, spu)
    queued = [t for t in active if t.status == db.QUEUED]
    live.publish(
        live.SYSTEM_CHANNEL,
        SystemStatus(
            running=len(active) - len(queued),
            waiting=len(queued),
            new_image_ready_in_s=forecast.new_image_ready_in_s,
        ),
    )

    positions = {t.id: position for position, t in enumerate(queued, start=1)}
    for t in active:
        # Progress and stage come from the worker; sending them here could move a bar backwards.
        update = TaskUpdate(id=t.id, status=t.status, ready_in_s=forecast.ready_in_s[t.id])
        if t.status == db.QUEUED:
            update.position = positions[t.id]
            update.queue_length = len(queued)
        live.publish(live.user_channel(t.user_id), update)

    live.publish(live.ADMIN_CHANNEL, overview.admin_overview())
