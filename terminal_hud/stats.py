"""System stats collection using psutil."""

import time
from dataclasses import dataclass

import psutil


@dataclass
class MemInfo:
    percent: float
    used_gb: float
    total_gb: float


@dataclass
class NetInfo:
    down_bps: float  # bytes per second
    up_bps: float


@dataclass
class SystemStats:
    cpu_percent: float
    memory: MemInfo
    network: NetInfo


class StatsCollector:
    """Collects system stats with delta tracking for network."""

    def __init__(self, interface: str | None = None):
        self.interface = interface
        self._prev_net = psutil.net_io_counters()
        self._prev_time = time.monotonic()
        # Prime CPU percent (first call always returns 0)
        psutil.cpu_percent(interval=None)

    def get_cpu(self) -> float:
        return psutil.cpu_percent(interval=None)

    def get_memory(self) -> MemInfo:
        vm = psutil.virtual_memory()
        return MemInfo(
            percent=vm.percent,
            used_gb=vm.used / (1024 ** 3),
            total_gb=vm.total / (1024 ** 3),
        )

    def get_network(self) -> NetInfo:
        now = time.monotonic()
        elapsed = now - self._prev_time
        if elapsed <= 0:
            return NetInfo(0.0, 0.0)

        current = psutil.net_io_counters()
        down_bps = (current.bytes_recv - self._prev_net.bytes_recv) / elapsed
        up_bps = (current.bytes_sent - self._prev_net.bytes_sent) / elapsed

        self._prev_net = current
        self._prev_time = now
        return NetInfo(down_bps=max(0.0, down_bps), up_bps=max(0.0, up_bps))

    def collect_all(self) -> SystemStats:
        return SystemStats(
            cpu_percent=self.get_cpu(),
            memory=self.get_memory(),
            network=self.get_network(),
        )
