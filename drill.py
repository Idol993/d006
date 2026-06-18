import uuid
import json
import os
from datetime import datetime
from typing import Optional
from models import DrillPlan, DrillStatus, Channel, RiskLevel
from compliance_log import ComplianceLogger
from pre_check import PreChecker


class DrillManager:
    def __init__(self, config: dict, logger: ComplianceLogger, pre_checker: PreChecker):
        self.config = config.get("drill", {})
        self.default_duration = self.config.get("default_duration", 3600)
        self.auto_recover = self.config.get("auto_recover", True)
        self.logger = logger
        self.pre_checker = pre_checker
        self.data_dir = config.get("system", {}).get("data_dir", "./data")
        os.makedirs(self.data_dir, exist_ok=True)

    def create_drill(
        self,
        name: str,
        target_version: str,
        rollback_version: str,
        channels: Optional[list] = None,
        operator: str = "system",
    ) -> DrillPlan:
        drill_id = f"DRILL-{uuid.uuid4().hex[:8].upper()}"
        if channels is None:
            channels = [ch.value for ch in Channel]

        drill = DrillPlan(
            drill_id=drill_id,
            name=name,
            target_version=target_version,
            rollback_version=rollback_version,
            channels=channels,
            planned_at=datetime.now(),
        )

        self.logger.log(
            "drill_created",
            operator,
            drill_id,
            f"创建回滚演练: {name}",
            {
                "drill_id": drill_id,
                "target_version": target_version,
                "rollback_version": rollback_version,
                "channels": channels,
            },
        )

        self._save_drill(drill)
        return drill

    def execute_drill(
        self,
        drill: DrillPlan,
        drill_content: Optional[str] = None,
        business_type: str = "理财",
        channel: str = "app",
        operator: str = "system",
        simulate: bool = True,
    ) -> DrillPlan:
        self.logger.log(
            "drill_start",
            operator,
            drill.drill_id,
            f"开始执行回滚演练: {drill.name}",
            {"target_version": drill.target_version, "rollback_version": drill.rollback_version},
        )

        drill.status = DrillStatus.RUNNING
        drill.started_at = datetime.now()

        self.logger.log(
            "drill_compliance_check",
            operator,
            drill.drill_id,
            "演练合规校验",
            {
                "business_type": business_type,
                "channel": channel,
                "content_preview": (drill_content[:50] + "...") if drill_content and len(drill_content) > 50 else (drill_content or ""),
            },
        )

        from models import ScriptVersion

        test_version = ScriptVersion(
            version=drill.target_version,
            content=drill_content if drill_content else f"演练目标话术版本{drill.target_version}",
            business_type=business_type,
            channel=drill.channels[0] if drill.channels else channel,
        )
        check_result = self.pre_checker.check(test_version, operator)
        drill.compliance_check_passed = check_result.passed
        drill.compliance_issues = (
            check_result.compliance_issues
            + check_result.regulatory_issues
            + check_result.info_protection_issues
        )

        if not drill.compliance_check_passed:
            self.logger.log(
                "drill_compliance_failed",
                operator,
                drill.drill_id,
                "演练合规校验未通过",
                {"issues": drill.compliance_issues[:10]},
            )
            drill.status = DrillStatus.FAILED
            drill.recovery_result = f"合规校验未通过，演练终止。共{len(drill.compliance_issues)}项问题"
            drill.channel_recovery_results = []
            drill.completed_at = datetime.now()
            self._save_drill(drill)
            return drill

        self.logger.log(
            "drill_simulate_rollback",
            operator,
            drill.drill_id,
            f"模拟回滚至版本: {drill.rollback_version}",
            {"channels": drill.channels},
        )

        if not simulate:
            import time
            time.sleep(min(self.default_duration, 5))

        channel_results = self._simulate_service_recovery(drill, operator)
        failed_channels = [c for c in channel_results if c.get("status") != "recovered"]
        drill.channel_recovery_results = channel_results

        if not failed_channels:
            drill.recovery_result = (
                f"所有{len(channel_results)}个渠道服务恢复正常，"
                f"话术已恢复至稳定版本{drill.rollback_version}"
            )
            drill.status = DrillStatus.COMPLETED
        else:
            drill.recovery_result = (
                f"部分渠道恢复异常，需人工介入。"
                f"异常渠道: {', '.join(c['channel'] for c in failed_channels)}"
            )
            drill.status = DrillStatus.FAILED
        drill.completed_at = datetime.now()

        self._save_drill(drill)

        self.logger.log(
            "drill_complete",
            operator,
            drill.drill_id,
            f"回滚演练完成: {drill.status.value}",
            {
                "compliance_passed": drill.compliance_check_passed,
                "recovery_result": drill.recovery_result,
                "channel_results": channel_results,
                "duration": (
                    (drill.completed_at - drill.started_at).total_seconds()
                    if drill.started_at and drill.completed_at
                    else 0
                ),
            },
        )
        return drill

    def _simulate_service_recovery(self, drill: DrillPlan, operator: str) -> list:
        channel_names = {"app": "APP", "phone": "电话", "wechat": "微信", "mini_program": "小程序"}
        self.logger.log(
            "drill_recovery_check",
            operator,
            drill.drill_id,
            "逐渠道模拟服务恢复",
            {"channels": drill.channels, "rollback_version": drill.rollback_version},
        )
        results = []
        for ch in drill.channels:
            recovered_at = datetime.now().isoformat()
            status = "recovered"
            detail = f"话术版本切换至{drill.rollback_version}，健康检查通过，服务响应恢复正常"
            self.logger.log(
                "drill_channel_recovery",
                operator,
                drill.drill_id,
                f"渠道{channel_names.get(ch, ch)}恢复成功",
                {"channel": ch, "status": status},
            )
            results.append({
                "channel": ch,
                "channel_name": channel_names.get(ch, ch),
                "status": status,
                "recovered_at": recovered_at,
                "restored_version": drill.rollback_version,
                "detail": detail,
            })
        return results

    def _save_drill(self, drill: DrillPlan):
        drill_dir = os.path.join(self.data_dir, "drills")
        os.makedirs(drill_dir, exist_ok=True)
        drill_file = os.path.join(drill_dir, f"{drill.drill_id}.json")
        drill_data = {
            "drill_id": drill.drill_id,
            "name": drill.name,
            "target_version": drill.target_version,
            "rollback_version": drill.rollback_version,
            "channels": drill.channels,
            "planned_at": drill.planned_at.isoformat(),
            "started_at": drill.started_at.isoformat() if drill.started_at else None,
            "completed_at": drill.completed_at.isoformat() if drill.completed_at else None,
            "status": drill.status.value,
            "compliance_check_passed": drill.compliance_check_passed,
            "compliance_issues": getattr(drill, "compliance_issues", []),
            "recovery_result": drill.recovery_result,
            "channel_recovery_results": getattr(drill, "channel_recovery_results", []),
        }
        with open(drill_file, "w", encoding="utf-8") as f:
            json.dump(drill_data, f, ensure_ascii=False, indent=2)

    def list_drills(self, status: Optional[DrillStatus] = None) -> list:
        drill_dir = os.path.join(self.data_dir, "drills")
        if not os.path.exists(drill_dir):
            return []
        results = []
        for filename in os.listdir(drill_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(drill_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if status and data.get("status") != status.value:
                continue
            results.append(data)
        return sorted(results, key=lambda x: x.get("planned_at", ""), reverse=True)
