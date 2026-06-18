import time
import random
import threading
from datetime import datetime
from typing import Optional, Callable
from collections import defaultdict
from models import (
    MonitorSnapshot,
    MonitorMetricType,
    Channel,
    PublishRecord,
)
from compliance_log import ComplianceLogger


class MonitorManager:
    def __init__(self, config: dict, logger: ComplianceLogger):
        monitor_cfg = config.get("monitor", {})
        self.interval = monitor_cfg.get("interval_seconds", 60)
        self.metrics_cfg = monitor_cfg.get("metrics", {})
        self.consecutive_limit = monitor_cfg.get("consecutive_violations", 3)
        self.logger = logger
        self._thread: Optional[threading.Thread] = None
        self._violation_counts: dict = defaultdict(lambda: defaultdict(int))
        self._callbacks: dict = defaultdict(list)
        self._stop_events: dict = {}
        self._locks: dict = defaultdict(threading.Lock)

    def start_monitoring(
        self,
        record: PublishRecord,
        on_threshold_exceeded: Optional[Callable] = None,
        simulate: bool = False,
    ):
        pid = record.publish_id
        with self._locks[pid]:
            self._violation_counts[pid] = defaultdict(int)
            self._callbacks[pid] = []
            if on_threshold_exceeded:
                self._callbacks[pid].append(on_threshold_exceeded)
            if pid in self._stop_events:
                self._stop_events[pid].set()
            stop_event = threading.Event()
            self._stop_events[pid] = stop_event

        self.logger.log(
            "monitor_start",
            "system",
            pid,
            f"启动服务监控，间隔{self.interval}秒",
        )

        def _monitor_loop():
            while not stop_event.is_set():
                for channel_key in ["app", "phone", "wechat", "mini_program"]:
                    snapshot = self._collect_metrics(
                        pid, channel_key, simulate
                    )
                    record.monitor_snapshots.append(snapshot)
                    violations = self._check_thresholds(record, snapshot, channel_key)
                    if violations:
                        self.logger.log(
                            "monitor_threshold_violation",
                            "system",
                            pid,
                            f"渠道{channel_key}指标异常: {violations}",
                            {
                                "channel": channel_key,
                                "violations": violations,
                                "snapshot": {
                                    "response_rate": snapshot.response_rate,
                                    "violation_rate": snapshot.violation_rate,
                                    "complaint_rate": snapshot.complaint_rate,
                                    "service_interruption": snapshot.service_interruption,
                                },
                            },
                        )
                        with self._locks[pid]:
                            callbacks = list(self._callbacks.get(pid, []))
                        for cb in callbacks:
                            try:
                                cb(record, channel_key, violations)
                            except Exception as e:
                                self.logger.log(
                                    "monitor_callback_error",
                                    "system",
                                    pid,
                                    f"监控回调执行失败: {e}",
                                    {"error": str(e)},
                                )
                            if record.status.value == "rolled_back":
                                return

                wait_interval = 0.5 if simulate else self.interval
                stop_event.wait(wait_interval)

        self._thread = threading.Thread(target=_monitor_loop, daemon=True)
        self._thread.start()

    def stop_monitoring(self, publish_id: str = ""):
        if publish_id and publish_id in self._stop_events:
            self._stop_events[publish_id].set()
            self.logger.log(
                "monitor_stop",
                "system",
                publish_id,
                "停止服务监控",
            )

    def _collect_metrics(
        self, publish_id: str, channel: str, simulate: bool = False
    ) -> MonitorSnapshot:
        if simulate:
            response_rate = random.uniform(0.80, 0.99)
            violation_rate = random.uniform(0.0, 0.05)
            complaint_rate = random.uniform(0.0, 0.03)
            service_interruption = random.choice([0, 0, 0, 0, 1])
        else:
            response_rate = self._fetch_response_rate(channel)
            violation_rate = self._fetch_violation_rate(channel)
            complaint_rate = self._fetch_complaint_rate(channel)
            service_interruption = self._fetch_service_interruption(channel)

        return MonitorSnapshot(
            timestamp=datetime.now(),
            response_rate=response_rate,
            violation_rate=violation_rate,
            complaint_rate=complaint_rate,
            service_interruption=service_interruption,
            channel=channel,
        )

    def _check_thresholds(self, record: PublishRecord, snapshot: MonitorSnapshot, channel: str) -> list:
        pid = record.publish_id
        violations = []
        for metric_key, metric_cfg in self.metrics_cfg.items():
            threshold = metric_cfg.get("threshold", 0)
            compare = metric_cfg.get("compare", "below")
            value = getattr(snapshot, metric_key, None)
            if value is None:
                continue
            exceeded = False
            if compare == "above" and value > threshold:
                exceeded = True
            elif compare == "below" and value < threshold:
                exceeded = True

            if exceeded:
                self._violation_counts[pid][metric_key] = (
                    self._violation_counts[pid].get(metric_key, 0) + 1
                )
                if self._violation_counts[pid][metric_key] >= self.consecutive_limit:
                    violations.append(
                        {
                            "metric": metric_key,
                            "value": value,
                            "threshold": threshold,
                            "description": metric_cfg.get("description", metric_key),
                            "consecutive": self._violation_counts[pid][metric_key],
                        }
                    )
            else:
                self._violation_counts[pid][metric_key] = 0

        return violations

    def _fetch_response_rate(self, channel: str) -> float:
        return 0.95

    def _fetch_violation_rate(self, channel: str) -> float:
        return 0.005

    def _fetch_complaint_rate(self, channel: str) -> float:
        return 0.003

    def _fetch_service_interruption(self, channel: str) -> int:
        return 0

    def is_running_for(self, publish_id: str) -> bool:
        if publish_id not in self._stop_events:
            return False
        return not self._stop_events[publish_id].is_set()

    def get_latest_snapshot(self, record: PublishRecord, channel: str = "") -> Optional[MonitorSnapshot]:
        if not record.monitor_snapshots:
            return None
        if channel:
            channel_snapshots = [s for s in record.monitor_snapshots if s.channel == channel]
            return channel_snapshots[-1] if channel_snapshots else None
        return record.monitor_snapshots[-1]
