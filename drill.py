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
        self, drill: DrillPlan, operator: str = "system", simulate: bool = True
    ) -> DrillPlan:
        self.logger.log(
            "drill_start",
            operator,
            drill.drill_id,
            f"开始执行回滚演练: {drill.name}",
        )

        drill.status = DrillStatus.RUNNING
        drill.started_at = datetime.now()

        self.logger.log(
            "drill_compliance_check",
            operator,
            drill.drill_id,
            "演练合规校验",
        )

        from models import ScriptVersion

        test_version = ScriptVersion(
            version=drill.target_version,
            content="[演练测试话术内容]",
            business_type="理财",
            channel=drill.channels[0] if drill.channels else "app",
        )
        check_result = self.pre_checker.check(test_version, operator)
        drill.compliance_check_passed = check_result.passed

        if not drill.compliance_check_passed:
            self.logger.log(
                "drill_compliance_failed",
                operator,
                drill.drill_id,
                "演练合规校验未通过",
                {"issues": check_result.compliance_issues + check_result.regulatory_issues},
            )
            drill.status = DrillStatus.FAILED
            drill.recovery_result = "合规校验未通过，演练终止"
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

        recovery_ok = self._simulate_service_recovery(drill, operator)
        drill.recovery_result = "服务恢复正常，话术已恢复至稳定版本" if recovery_ok else "服务恢复异常，需人工介入"

        drill.status = DrillStatus.COMPLETED if recovery_ok else DrillStatus.FAILED
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
                "duration": (
                    (drill.completed_at - drill.started_at).total_seconds()
                    if drill.started_at and drill.completed_at
                    else 0
                ),
            },
        )
        return drill

    def _simulate_service_recovery(self, drill: DrillPlan, operator: str) -> bool:
        self.logger.log(
            "drill_recovery_check",
            operator,
            drill.drill_id,
            "检查服务恢复状态",
            {"channels": drill.channels},
        )
        for channel in drill.channels:
            self.logger.log(
                "drill_channel_recovery",
                operator,
                drill.drill_id,
                f"渠道{channel}服务恢复检查",
                {"channel": channel, "status": "recovered"},
            )
        return True

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
            "recovery_result": drill.recovery_result,
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
