"""Changes how many worker containers run."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.main import ControllerState


def autoscale_tick(now: float, state: ControllerState) -> None:
    """Leaves the number of workers as it is."""
