import time
from datetime import datetime
from typing import Optional, Callable
from models import (
    Channel,
    GrayReleaseStage,
    PublishRecord,
    PublishStatus,
    RiskLevel,
)
from compliance_log import ComplianceLogger


class GrayReleaseManager:
    def __init__(self, config: dict, logger: ComplianceLogger):
        self.config = config.get("gray_release", {})
        self.channels = self.config.get("channels", ["app", "phone", "wechat", "mini_program"])
        self.channel_names = self.config.get("channel_names", {})
        self.stages = self.config.get("stages", [])
        self.logger = logger

    def execute_gray_release(
        self,
        record: PublishRecord,
        on_stage_complete: Optional[Callable] = None,
        on_monitor_check: Optional[Callable] = None,
        simulate: bool = False,
    ) -> PublishRecord:
        operator = record.operator
        self.logger.log(
            "gray_release_start",
            operator,
            record.publish_id,
            "开始渠道灰度发布",
            {"channels": self.channels},
        )

        record.status = PublishStatus.GRAY_RELEASING

        for channel_key in self.channels:
            channel = Channel(channel_key)
            channel_name = self.channel_names.get(channel_key, channel_key)

            self.logger.log(
                "gray_release_channel_start",
                operator,
                record.publish_id,
                f"开始渠道灰度: {channel_name}",
                {"channel": channel_key},
            )

            for stage_cfg in self.stages:
                ratio = stage_cfg["ratio"]
                duration = stage_cfg["duration"]

                stage = GrayReleaseStage(
                    channel=channel,
                    ratio=ratio,
                    duration_minutes=duration,
                    started_at=datetime.now(),
                )

                self.logger.log(
                    "gray_release_stage",
                    operator,
                    record.publish_id,
                    f"渠道{channel_name}灰度比例: {ratio*100:.0f}%, 持续: {duration}分钟",
                    {"channel": channel_key, "ratio": ratio, "duration": duration},
                )

                if not simulate:
                    if duration > 0:
                        time.sleep(duration * 60)
                elif simulate:
                    time.sleep(0.1)

                if on_monitor_check:
                    should_rollback = on_monitor_check(record, channel_key)
                    if should_rollback:
                        self.logger.log(
                            "gray_release_aborted",
                            operator,
                            record.publish_id,
                            f"渠道{channel_name}灰度发布中止，监控异常",
                            {"channel": channel_key, "ratio": ratio},
                        )
                        return record

                stage.completed_at = datetime.now()
                record.gray_stages.append(stage)

                if on_stage_complete:
                    on_stage_complete(record, stage)

            self.logger.log(
                "gray_release_channel_complete",
                operator,
                record.publish_id,
                f"渠道{channel_name}灰度发布完成",
                {"channel": channel_key},
            )

        record.status = PublishStatus.PUBLISHED
        record.published_at = datetime.now()
        self.logger.log(
            "gray_release_complete",
            operator,
            record.publish_id,
            "全渠道灰度发布完成，话术已上线",
        )
        return record

    def get_current_stage(self, record: PublishRecord) -> Optional[GrayReleaseStage]:
        if not record.gray_stages:
            return None
        for stage in reversed(record.gray_stages):
            if stage.completed_at is None:
                return stage
        return record.gray_stages[-1] if record.gray_stages else None

    def get_release_progress(self, record: PublishRecord) -> dict:
        total_stages = len(self.channels) * len(self.stages)
        completed_stages = len(record.gray_stages)
        return {
            "total_stages": total_stages,
            "completed_stages": completed_stages,
            "progress": completed_stages / total_stages if total_stages > 0 else 0,
            "current_channel": (
                record.gray_stages[-1].channel.value if record.gray_stages else None
            ),
        }
