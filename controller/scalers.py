"""How the autoscaler reads and changes the number of worker containers."""

import logging
from typing import Protocol

import docker
import docker.errors

from common.config import get_settings

log = logging.getLogger(__name__)


class ScalerError(RuntimeError):
    pass


class Scaler(Protocol):
    def replicas(self) -> int: ...

    def set_replicas(self, n: int) -> None: ...


class NullScaler:
    """Keeps the count in memory and changes nothing, for running without Swarm."""

    def __init__(self, replicas: int):
        self._replicas = replicas

    def replicas(self) -> int:
        return self._replicas

    def set_replicas(self, n: int) -> None:
        log.info("would scale the workers from %d to %d", self._replicas, n)
        self._replicas = n


class SwarmScaler:
    """The replica count of a Swarm service, through the Docker socket of a manager node."""

    def __init__(self, service: str, client: docker.DockerClient | None = None):
        self.service = service
        if client is None:
            try:
                client = docker.from_env()
            except docker.errors.DockerException as exc:
                raise ScalerError(f"cannot reach Docker to scale {service}: {exc}") from exc
        self.client = client

    def _service(self):
        # Fetched every time: scaling needs the service's current version, and someone may have changed it.
        try:
            return self.client.services.get(self.service)
        except docker.errors.NotFound as exc:
            raise ScalerError(f"no Swarm service named {self.service}; is the stack deployed?") from exc
        except docker.errors.APIError as exc:
            if exc.status_code == 503:
                raise ScalerError(
                    f"cannot read {self.service}: the controller must run on a Swarm manager node"
                ) from exc
            raise

    def replicas(self) -> int:
        try:
            return self._service().attrs["Spec"]["Mode"]["Replicated"]["Replicas"]
        except KeyError as exc:
            raise ScalerError(f"{self.service} is not a replicated service") from exc

    def set_replicas(self, n: int) -> None:
        self._service().scale(n)


def from_settings() -> Scaler:
    settings = get_settings()
    if settings.SCALER == "swarm":
        return SwarmScaler(settings.WORKER_SERVICE)
    return NullScaler(settings.MIN_WORKERS)
