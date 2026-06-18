import json
import os
import uuid
from datetime import datetime
from typing import Optional
from models import (
    RollbackReport,
    RollbackTrigger,
    PublishRecord,
    PublishStatus,
    Channel,
    MonitorMetricType,
)
from compliance_log import ComplianceLogger


class RollbackManager:
    def __init__(self, config: dict, logger: ComplianceLogger, data_dir: str = "./data"):
        self.config = config.get("rollback", {})
        self.notify_roles = self.config.get("notify_roles", ["运营", "客服", "合规", "舆情监控"])
        self.logger = logger
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def execute_rollback(
        self,
        record: PublishRecord,
        trigger: RollbackTrigger,
        violation_reasons: Optional[list] = None,
        operator: str = "system",
    ) -> RollbackReport:
        rollback_id = f"RB-{uuid.uuid4().hex[:8].upper()}"
        self.logger.log(
            "rollback_start",
            operator,
            record.publish_id,
            f"开始服务回滚，触发方式: {trigger.value}",
            {"rollback_id": rollback_id, "trigger": trigger.value},
        )

        channel_impact = self._assess_channel_impact(record)
        complaint_stats = self._collect_complaint_stats(record)

        report = RollbackReport(
            rollback_id=rollback_id,
            publish_id=record.publish_id,
            trigger=trigger,
            channel_impact=channel_impact,
            violation_reasons=violation_reasons or self._determine_violation_reasons(record),
            complaint_stats=complaint_stats,
            rolled_back_at=datetime.now(),
            restored_version=self._find_stable_version(record),
            notified_roles=self.notify_roles,
        )

        self._restore_previous_version(record, report.restored_version, operator)
        record.status = PublishStatus.ROLLED_BACK
        record.rolled_back_at = datetime.now()
        record.rollback_report = report

        self._notify_stakeholders(report, operator)

        self._save_report(report)

        self.logger.log(
            "rollback_complete",
            operator,
            record.publish_id,
            f"服务回滚完成，恢复至版本: {report.restored_version}",
            {
                "rollback_id": rollback_id,
                "restored_version": report.restored_version,
                "channel_impact": channel_impact,
                "violation_reasons": report.violation_reasons,
            },
        )
        return report

    def _assess_channel_impact(self, record: PublishRecord) -> list:
        impact = []
        channel_names = {"app": "APP", "phone": "电话", "wechat": "微信", "mini_program": "小程序"}
        for stage in record.gray_stages:
            channel_key = stage.channel.value
            impact.append(
                {
                    "channel": channel_key,
                    "channel_name": channel_names.get(channel_key, channel_key),
                    "gray_ratio": stage.ratio,
                    "started_at": stage.started_at.isoformat() if stage.started_at else None,
                    "status": "受影响",
                }
            )
        seen = set()
        unique_impact = []
        for item in impact:
            if item["channel"] not in seen:
                seen.add(item["channel"])
                unique_impact.append(item)
        return unique_impact

    def _collect_complaint_stats(self, record: PublishRecord) -> dict:
        stats = {"total": 0, "by_channel": {}, "by_type": {}}
        for snapshot in record.monitor_snapshots:
            ch = snapshot.channel
            if ch not in stats["by_channel"]:
                stats["by_channel"][ch] = 0
            if snapshot.complaint_rate > 0:
                count = int(snapshot.complaint_rate * 1000)
                stats["by_channel"][ch] += count
                stats["total"] += count
        stats["by_type"] = {
            "话术违规": sum(1 for s in record.monitor_snapshots if s.violation_rate > 0.02),
            "服务中断": sum(1 for s in record.monitor_snapshots if s.service_interruption > 0),
        }
        return stats

    def _determine_violation_reasons(self, record: PublishRecord) -> list:
        reasons = []
        for snapshot in record.monitor_snapshots:
            if snapshot.violation_rate > 0.02:
                reasons.append(f"渠道{snapshot.channel}话术违规率{snapshot.violation_rate:.2%}超过阈值2%")
            if snapshot.complaint_rate > 0.01:
                reasons.append(f"渠道{snapshot.channel}客户投诉率{snapshot.complaint_rate:.2%}超过阈值1%")
            if snapshot.service_interruption > 0:
                reasons.append(f"渠道{snapshot.channel}检测到{snapshot.service_interruption}次服务中断")
        return list(set(reasons))

    def _find_stable_version(self, record: PublishRecord) -> str:
        versions_file = os.path.join(self.data_dir, "stable_versions.json")
        if os.path.exists(versions_file):
            with open(versions_file, "r", encoding="utf-8") as f:
                versions = json.load(f)
            channel = record.script_version.channel
            business_type = record.script_version.business_type
            for v in reversed(versions):
                if v.get("channel") == channel and v.get("business_type") == business_type:
                    if v.get("version") != record.script_version.version:
                        return v["version"]
        ver_parts = record.script_version.version.split(".")
        if len(ver_parts) >= 3:
            patch = int(ver_parts[-1])
            ver_parts[-1] = str(max(0, patch - 1))
            return ".".join(ver_parts)
        return f"{record.script_version.version}-stable"

    def _restore_previous_version(
        self, record: PublishRecord, stable_version: str, operator: str
    ):
        self.logger.log(
            "version_restore",
            operator,
            record.publish_id,
            f"恢复至稳定合规话术版本: {stable_version}",
            {"restored_version": stable_version},
        )
        versions_file = os.path.join(self.data_dir, "stable_versions.json")
        versions = []
        if os.path.exists(versions_file):
            with open(versions_file, "r", encoding="utf-8") as f:
                versions = json.load(f)
        versions.append(
            {
                "version": record.script_version.version,
                "channel": record.script_version.channel,
                "business_type": record.script_version.business_type,
                "status": "rolled_back",
                "rolled_back_at": datetime.now().isoformat(),
            }
        )
        with open(versions_file, "w", encoding="utf-8") as f:
            json.dump(versions, f, ensure_ascii=False, indent=2)

    def _notify_stakeholders(self, report: RollbackReport, operator: str):
        for role in report.notified_roles:
            self.logger.log(
                "rollback_notification",
                operator,
                report.rollback_id,
                f"通知干系人: {role}",
                {
                    "role": role,
                    "rollback_id": report.rollback_id,
                    "violation_reasons": report.violation_reasons,
                },
            )

    def _save_report(self, report: RollbackReport):
        report_dir = os.path.join(self.data_dir, "rollback_reports")
        os.makedirs(report_dir, exist_ok=True)
        report_file = os.path.join(report_dir, f"{report.rollback_id}.json")
        report_data = {
            "rollback_id": report.rollback_id,
            "publish_id": report.publish_id,
            "trigger": report.trigger.value,
            "channel_impact": report.channel_impact,
            "violation_reasons": report.violation_reasons,
            "complaint_stats": report.complaint_stats,
            "rolled_back_at": report.rolled_back_at.isoformat(),
            "restored_version": report.restored_version,
            "notified_roles": report.notified_roles,
        }
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)

    def get_report(self, rollback_id: str) -> Optional[dict]:
        report_file = os.path.join(
            self.data_dir, "rollback_reports", f"{rollback_id}.json"
        )
        if os.path.exists(report_file):
            with open(report_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return None
