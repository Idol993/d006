import os
import sys
import uuid
import yaml
import json
import argparse
from datetime import datetime, timedelta
from typing import Optional

from models import (
    ScriptVersion,
    PreCheckResult,
    ApprovalFlow,
    ApprovalStatus,
    PublishRecord,
    PublishStatus,
    RiskLevel,
    Channel,
    RollbackTrigger,
    RollbackReport,
    DrillPlan,
    DrillStatus,
)
from compliance_log import ComplianceLogger
from pre_check import PreChecker
from approval import ApprovalManager
from gray_release import GrayReleaseManager
from monitor import MonitorManager
from rollback import RollbackManager
from drill import DrillManager
from report import ReportManager
from history import HistoryManager


class ComplianceScriptManager:
    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        self.logger = ComplianceLogger(self.config)
        self.pre_checker = PreChecker(self.config, self.logger)
        self.approval_mgr = ApprovalManager(self.config, self.logger)
        self.gray_release_mgr = GrayReleaseManager(self.config, self.logger)
        self.monitor_mgr = MonitorManager(self.config, self.logger)
        data_dir = self.config.get("system", {}).get("data_dir", "./data")
        self.rollback_mgr = RollbackManager(self.config, self.logger, data_dir)
        self.drill_mgr = DrillManager(self.config, self.logger, self.pre_checker)
        self.report_mgr = ReportManager(self.config, self.logger)
        self.history_mgr = HistoryManager(self.config, self.logger)
        self._ensure_dirs()

    def _load_config(self, path: str) -> dict:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        return {}

    def _ensure_dirs(self):
        for key in ["data_dir", "log_dir", "report_dir", "export_dir"]:
            d = self.config.get("system", {}).get(key, f"./{key.replace('_dir', '')}")
            os.makedirs(d, exist_ok=True)

    def submit_publish_request(
        self,
        version: str,
        content: str,
        business_type: str,
        channel: str,
        risk_level: str,
        operator: str,
    ) -> PublishRecord:
        script = ScriptVersion(
            version=version,
            content=content,
            business_type=business_type,
            channel=channel,
            created_at=datetime.now(),
            checksum=PreChecker.compute_checksum(content),
        )
        record = PublishRecord(
            publish_id=f"PUB-{uuid.uuid4().hex[:8].upper()}",
            script_version=script,
            risk_level=RiskLevel(risk_level),
            status=PublishStatus.PENDING_CHECK,
            operator=operator,
            submitted_at=datetime.now(),
        )

        self.logger.log(
            "publish_request_submitted",
            operator,
            record.publish_id,
            f"话术发布申请已提交，版本: {version}，风险级别: {risk_level}",
            {
                "version": version,
                "business_type": business_type,
                "channel": channel,
                "risk_level": risk_level,
            },
        )

        pre_check_result = self.pre_checker.check(script, operator)
        record.pre_check_result = pre_check_result

        if not pre_check_result.passed:
            record.status = PublishStatus.CHECK_FAILED
            self.logger.log(
                "publish_check_failed",
                operator,
                record.publish_id,
                "前置条件检查未通过，发布终止",
                {"issues": pre_check_result.compliance_issues + pre_check_result.regulatory_issues + pre_check_result.info_protection_issues + pre_check_result.service_issues},
            )
            self.history_mgr.save_record(record)
            return record

        record.status = PublishStatus.PENDING_APPROVAL
        approval_flow = self.approval_mgr.create_approval_flow(record.risk_level, operator)
        record.approval_flow = approval_flow

        for node in approval_flow.nodes:
            self.approval_mgr.approve_node(
                approval_flow, node.role, node.approver,
                comment="自动审批通过（模拟）", operator=operator,
            )

        if self.approval_mgr.is_flow_approved(approval_flow):
            self.logger.log(
                "publish_approved",
                operator,
                record.publish_id,
                "审批流程全部通过，准备灰度发布",
            )
            self._execute_publish(record, operator)
        else:
            record.status = PublishStatus.APPROVAL_REJECTED
            self.logger.log(
                "publish_rejected",
                operator,
                record.publish_id,
                "审批流程被拒绝",
            )

        self.history_mgr.save_record(record)
        return record

    def _execute_publish(self, record: PublishRecord, operator: str):
        rolled_back = {"flag": False}

        def on_threshold_exceeded(rec, channel_key, violations):
            if rolled_back["flag"] or rec.status == PublishStatus.ROLLED_BACK:
                return
            rolled_back["flag"] = True
            self._trigger_auto_rollback(rec, violations, operator)

        def on_monitor_check(rec, channel_key):
            if rolled_back["flag"]:
                return True
            snapshot = self.monitor_mgr.get_latest_snapshot(rec, channel_key)
            if snapshot:
                violations = self.monitor_mgr._check_thresholds(rec, snapshot, channel_key)
                if violations and self.config.get("rollback", {}).get("auto_rollback", True):
                    self._trigger_auto_rollback(rec, violations, operator)
                    rolled_back["flag"] = True
                    return True
            return False

        self.monitor_mgr.start_monitoring(
            record,
            on_threshold_exceeded=on_threshold_exceeded,
            simulate=True,
        )

        record = self.gray_release_mgr.execute_gray_release(
            record,
            on_monitor_check=on_monitor_check,
            simulate=True,
        )

        if record.status == PublishStatus.PUBLISHED:
            self.logger.log(
                "publish_monitoring_active",
                operator,
                record.publish_id,
                "灰度完成，正式发布，持续监控中",
            )

    def _trigger_auto_rollback(self, record: PublishRecord, violations: list, operator: str):
        if record.status == PublishStatus.ROLLED_BACK:
            return
        self.logger.log(
            "auto_rollback_triggered",
            operator,
            record.publish_id,
            "监控指标超过阈值，触发自动回滚",
            {"violations": violations},
        )
        self.monitor_mgr.stop_monitoring(record.publish_id)

        violation_reasons = [
            f"{v['description']}: 当前值{v['value']:.4f}, 阈值{v['threshold']}"
            for v in violations
        ]
        report = self.rollback_mgr.execute_rollback(
            record,
            trigger=RollbackTrigger.AUTO,
            violation_reasons=violation_reasons,
            operator=operator,
        )
        self._save_stable_version(record, report.restored_version, operator)

        self.logger.log(
            "auto_rollback_complete",
            operator,
            record.publish_id,
            f"自动回滚完成，恢复版本: {report.restored_version}",
            {
                "restored_version": report.restored_version,
                "channels_affected": [c["channel"] for c in report.channel_impact],
            },
        )
        self.history_mgr.save_record(record)

        stable_record = self._rehydrate_stable_record(record, report.restored_version)
        if stable_record:
            def _stable_callback(rec, ch, v):
                pass
            self.monitor_mgr.start_monitoring(
                stable_record,
                on_threshold_exceeded=_stable_callback,
                simulate=True,
            )

    def manual_rollback(self, publish_id: str, operator: str, reason: str = "") -> Optional[RollbackReport]:
        target = None
        all_records = self.history_mgr._load_all_records()
        for r in all_records:
            if r.get("publish_id") == publish_id:
                target = r
                break

        if not target:
            self.logger.log("manual_rollback_failed", operator, publish_id, "未找到发布记录")
            return None

        script = ScriptVersion(
            version=target["script_version"]["version"],
            content=target["script_version"]["content"],
            business_type=target["script_version"]["business_type"],
            channel=target["script_version"]["channel"],
        )
        record = PublishRecord(
            publish_id=target["publish_id"],
            script_version=script,
            risk_level=RiskLevel(target["risk_level"]),
            status=PublishStatus(target["status"]),
            operator=target["operator"],
            submitted_at=datetime.fromisoformat(target["submitted_at"]),
        )

        self.monitor_mgr.stop_monitoring(publish_id)
        report = self.rollback_mgr.execute_rollback(
            record,
            trigger=RollbackTrigger.MANUAL,
            violation_reasons=[reason] if reason else [],
            operator=operator,
        )
        self._save_stable_version(record, report.restored_version, operator)
        self.history_mgr.save_record(record)
        return report

    def create_drill(
        self,
        name: str,
        target_version: str,
        rollback_version: str,
        channels: Optional[list] = None,
        drill_content: Optional[str] = None,
        business_type: str = "理财",
        channel: str = "app",
        operator: str = "system",
    ) -> DrillPlan:
        drill = self.drill_mgr.create_drill(
            name=name,
            target_version=target_version,
            rollback_version=rollback_version,
            channels=channels,
            operator=operator,
        )
        resolved_content = drill_content if drill_content else self._find_version_content(target_version, business_type, channel)
        drill = self.drill_mgr.execute_drill(
            drill,
            drill_content=resolved_content,
            business_type=business_type,
            channel=channel,
            operator=operator,
            simulate=True,
        )
        return drill

    def get_stable_version(self, channel: Optional[str] = None, business_type: Optional[str] = None) -> list:
        data_dir = self.config.get("system", {}).get("data_dir", "./data")
        stable_file = os.path.join(data_dir, "stable_versions.json")
        if not os.path.exists(stable_file):
            return []
        with open(stable_file, "r", encoding="utf-8") as f:
            all_stable = json.load(f)
        filtered = [s for s in all_stable if s.get("status") == "stable"]
        if channel:
            filtered = [s for s in filtered if s.get("channel") == channel]
        if business_type:
            filtered = [s for s in filtered if s.get("business_type") == business_type]
        latest: dict = {}
        for s in filtered:
            key = (s.get("channel"), s.get("business_type"))
            existing = latest.get(key)
            ts = s.get("set_at", "")
            if not existing or ts > existing.get("set_at", ""):
                latest[key] = s
        return sorted(latest.values(), key=lambda x: x.get("set_at", ""), reverse=True)

    def _save_stable_version(self, record: PublishRecord, restored_version: str, operator: str):
        data_dir = self.config.get("system", {}).get("data_dir", "./data")
        stable_file = os.path.join(data_dir, "stable_versions.json")
        all_stable = []
        if os.path.exists(stable_file):
            with open(stable_file, "r", encoding="utf-8") as f:
                all_stable = json.load(f)
        sv = record.script_version
        now_str = datetime.now().isoformat()
        new_entry = {
            "version": restored_version,
            "channel": sv.channel,
            "business_type": sv.business_type,
            "status": "stable",
            "replaces_publish_id": record.publish_id,
            "set_by": operator,
            "set_at": now_str,
        }
        for s in all_stable:
            if (
                s.get("channel") == sv.channel
                and s.get("business_type") == sv.business_type
                and s.get("status") == "stable"
                and s.get("set_at") < now_str
            ):
                s["status"] = "superseded"
        all_stable.append(new_entry)
        with open(stable_file, "w", encoding="utf-8") as f:
            json.dump(all_stable, f, ensure_ascii=False, indent=2)
        self.logger.log(
            "stable_version_set",
            operator,
            restored_version,
            f"更新稳定版本: {sv.channel}/{sv.business_type} -> {restored_version}",
        )

    def _rehydrate_stable_record(self, original_record: PublishRecord, restored_version: str) -> Optional[PublishRecord]:
        stable_list = self.get_stable_version(
            channel=original_record.script_version.channel,
            business_type=original_record.script_version.business_type,
        )
        if not stable_list:
            return None
        version_content = self._find_version_content(
            restored_version,
            original_record.script_version.business_type,
            original_record.script_version.channel,
        )
        if not version_content:
            return None
        stable_script = ScriptVersion(
            version=restored_version,
            content=version_content,
            business_type=original_record.script_version.business_type,
            channel=original_record.script_version.channel,
            created_at=datetime.now(),
            checksum=PreChecker.compute_checksum(version_content),
        )
        return PublishRecord(
            publish_id=f"STABLE-{uuid.uuid4().hex[:8].upper()}",
            script_version=stable_script,
            risk_level=original_record.risk_level,
            status=PublishStatus.PUBLISHED,
            operator="system",
            submitted_at=datetime.now(),
            published_at=datetime.now(),
        )

    def _find_version_content(self, version: str, business_type: str, channel: str) -> str:
        all_records = self.history_mgr._load_all_records()
        for r in all_records:
            sv = r.get("script_version", {})
            if (
                sv.get("version") == version
                and sv.get("business_type") == business_type
                and sv.get("channel") == channel
            ):
                return sv.get("content", "")
        default_templates = {
            "理财": (
                f"尊敬的客户，感谢您选择我们的理财产品（版本{version}）。"
                "请注意投资有风险，入市需谨慎。"
                "风险提示：过往业绩不代表未来表现。"
                "投资者适当性：本产品适合风险承受能力为中高风险的投资者。"
            ),
            "贷款": (
                f"尊敬的客户，关于贷款产品（版本{version}），综合年化利率以实际审批为准。"
                "风险提示：逾期可能影响个人征信记录，请按时还款。"
            ),
            "保险": (
                f"尊敬的客户，保险产品（版本{version}）请仔细阅读免责条款。"
                "犹豫期说明：签收之日起15日内可全额退保。"
            ),
            "信用卡": (
                f"尊敬的客户，信用卡（版本{version}）年费标准以卡片等级为准。"
                "计息规则：未全额还款时按日计息，年利率约18.25%。"
            ),
        }
        return default_templates.get(business_type, f"标准话术内容（版本{version}）")

    def schedule_weekly_report(self, operator: str = "system"):
        import sched
        import time as _time

        scheduler = sched.scheduler(_time.time, _time.sleep)
        self.logger.log("weekly_scheduler_start", operator, "scheduler", "启动周报自动调度（每周一0点）")
        print("周报调度已启动，将在每周一自动生成报表。按 Ctrl+C 退出。")

        def _next_monday(now: datetime) -> datetime:
            days_ahead = 7 - now.weekday()
            if days_ahead == 7:
                days_ahead = 0
            nxt = now + timedelta(days=days_ahead)
            return nxt.replace(hour=0, minute=0, second=0, microsecond=0)

        def _loop(sc: sched.scheduler):
            try:
                now = datetime.now()
                nxt = _next_monday(now)
                if nxt <= now:
                    nxt += timedelta(days=7)
                delay = (nxt - now).total_seconds()
                self.logger.log(
                    "weekly_scheduler_next",
                    operator,
                    "scheduler",
                    f"下次报表生成: {nxt.isoformat()} (延迟 {delay:.0f}s)",
                )
                sc.enter(delay, 1, _run_report, (sc, nxt))
            except Exception as e:
                self.logger.log(
                    "weekly_scheduler_error",
                    operator,
                    "scheduler",
                    f"调度循环出错: {e}",
                )

        def _run_report(sc: sched.scheduler, scheduled_at: datetime):
            try:
                self.logger.log(
                    "weekly_scheduler_run",
                    operator,
                    "scheduler",
                    f"触发自动周报生成 (计划 {scheduled_at.isoformat()})",
                )
                result = self.generate_weekly_report(operator=operator)
                self.logger.log(
                    "weekly_scheduler_done",
                    operator,
                    "scheduler",
                    f"自动周报生成完成: PDF={result['pdf_report']}, Excel={result['excel_report']}",
                )
            except Exception as e:
                self.logger.log(
                    "weekly_scheduler_fail",
                    operator,
                    "scheduler",
                    f"自动周报生成失败: {e}",
                )
            finally:
                _loop(sc)

        _loop(scheduler)
        try:
            scheduler.run()
        except KeyboardInterrupt:
            self.logger.log("weekly_scheduler_stop", operator, "scheduler", "用户停止周报调度")
            print("周报调度已停止。")

    def generate_weekly_report(self, operator: str = "system") -> dict:
        all_records = self.history_mgr._load_all_records()
        records = []
        for r in all_records:
            script = ScriptVersion(
                version=r["script_version"]["version"],
                content=r["script_version"].get("content", ""),
                business_type=r["script_version"]["business_type"],
                channel=r["script_version"]["channel"],
            )
            status = PublishStatus(r.get("status", "pending_check"))
            risk = RiskLevel(r.get("risk_level", "routine"))
            rec = PublishRecord(
                publish_id=r["publish_id"],
                script_version=script,
                risk_level=risk,
                status=status,
                operator=r.get("operator", ""),
                submitted_at=datetime.fromisoformat(r["submitted_at"]),
            )
            records.append(rec)

        stats = self.report_mgr.generate_weekly_stats(records, operator=operator)
        pdf_path = self.report_mgr.generate_pdf_report(stats, operator)
        excel_path = self.report_mgr.generate_excel_report(stats, operator)

        return {
            "stats": {
                "period": f"{stats.period_start.strftime('%Y-%m-%d')} ~ {stats.period_end.strftime('%Y-%m-%d')}",
                "total_publishes": stats.total_publishes,
                "success_publishes": stats.success_publishes,
                "publish_success_rate": f"{stats.publish_success_rate:.1%}",
                "rollback_count": stats.rollback_count,
                "avg_customer_satisfaction": f"{stats.avg_customer_satisfaction:.1%}",
            },
            "pdf_report": pdf_path,
            "excel_report": excel_path,
        }

    def query_history(
        self,
        publish_time_start: Optional[str] = None,
        publish_time_end: Optional[str] = None,
        channel: Optional[str] = None,
        business_type: Optional[str] = None,
        version: Optional[str] = None,
        status: Optional[str] = None,
        risk_level: Optional[str] = None,
        operator: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        start = datetime.fromisoformat(publish_time_start) if publish_time_start else None
        end = datetime.fromisoformat(publish_time_end) if publish_time_end else None
        return self.history_mgr.query_records(
            publish_time_start=start,
            publish_time_end=end,
            channel=channel,
            business_type=business_type,
            version=version,
            status=status,
            risk_level=risk_level,
            operator=operator,
            limit=limit,
        )

    def batch_export(
        self,
        records: list,
        format: str = "csv",
        filename: Optional[str] = None,
        operator: str = "system",
    ) -> str:
        return self.history_mgr.batch_export(records, format, filename, operator)

    def query_compliance_logs(
        self,
        action: Optional[str] = None,
        operator: Optional[str] = None,
        target: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        st = datetime.fromisoformat(start_time) if start_time else None
        et = datetime.fromisoformat(end_time) if end_time else None
        return self.logger.query(
            action=action,
            operator=operator,
            target=target,
            start_time=st,
            end_time=et,
            limit=limit,
        )


def run_demo():
    print("=" * 70)
    print("  金融智能客服合规话术发布与服务回滚 - 自动化管理演示（增强版）")
    print("=" * 70)

    mgr = ComplianceScriptManager("config.yaml")
    import time

    print("\n【场景1】常规话术更新 - 完整发布流程（带监控回滚）")
    print("-" * 50)
    sample_content = (
        "尊敬的客户，感谢您选择我们的理财产品。"
        "请注意投资有风险，入市需谨慎。"
        "风险提示：过往业绩不代表未来表现。"
        "投资者适当性：本产品适合风险承受能力为中高风险的投资者。"
    )
    record = mgr.submit_publish_request(
        version="2.1.0",
        content=sample_content,
        business_type="理财",
        channel="app",
        risk_level="routine",
        operator="运营-张三",
    )
    time.sleep(3)
    print(f"  发布ID: {record.publish_id}")
    print(f"  最终状态: {record.status.value}")
    if record.pre_check_result:
        print(f"  前置检查: {'通过' if record.pre_check_result.passed else '未通过'}")
    if record.approval_flow:
        summary = mgr.approval_mgr.get_flow_summary(record.approval_flow)
        print(f"  审批状态: {summary['status']}")
    if record.rollback_report:
        print(f"  【自动回滚】回滚报告ID: {record.rollback_report.rollback_id}")
        print(f"  【自动回滚】恢复版本: {record.rollback_report.restored_version}")
        print(f"  【自动回滚】违规原因: {record.rollback_report.violation_reasons[:2]}")
        print(f"  【自动回滚】影响渠道: {[c['channel_name'] for c in record.rollback_report.channel_impact]}")

    print("\n【场景2】话术合规不通过 - 发布被拒绝")
    print("-" * 50)
    bad_content = (
        "尊敬的客户，我们的理财产品保证收益，零风险，稳赚不赔！"
        "绝对安全，保本保息，年化收益率8%以上！"
    )
    record2 = mgr.submit_publish_request(
        version="2.2.0",
        content=bad_content,
        business_type="理财",
        channel="app",
        risk_level="routine",
        operator="运营-李四",
    )
    print(f"  发布ID: {record2.publish_id}")
    print(f"  最终状态: {record2.status.value}")
    if record2.pre_check_result and not record2.pre_check_result.passed:
        all_issues = (
            record2.pre_check_result.compliance_issues
            + record2.pre_check_result.regulatory_issues
            + record2.pre_check_result.info_protection_issues
        )
        print(f"  合规问题 ({len(all_issues)}):")
        for issue in all_issues[:5]:
            print(f"    - {issue}")

    print("\n【场景3】紧急合规整改 - 高风险发布")
    print("-" * 50)
    emergency_content = (
        "尊敬的客户，关于近期监管政策调整，现更正话术如下："
        "风险提示：贷款有成本，逾期可能影响个人征信。"
        "投资者适当性：贷款产品需评估还款能力后方可办理。"
        "综合年化利率以实际审批为准。逾期后果：逾期还款将产生罚息并影响信用记录。"
    )
    record3 = mgr.submit_publish_request(
        version="3.0.0",
        content=emergency_content,
        business_type="贷款",
        channel="phone",
        risk_level="emergency_compliance",
        operator="合规-王五",
    )
    time.sleep(2)
    print(f"  发布ID: {record3.publish_id}")
    print(f"  最终状态: {record3.status.value}")
    if record3.status == PublishStatus.CHECK_FAILED and record3.pre_check_result:
        issues = (
            record3.pre_check_result.compliance_issues
            + record3.pre_check_result.regulatory_issues
            + record3.pre_check_result.info_protection_issues
        )
        print(f"  检查问题: {issues[:3]}")
    if record3.rollback_report:
        print(f"  【自动回滚】恢复版本: {record3.rollback_report.restored_version}")

    print("\n【场景4】创建回滚演练 - 使用合规话术，逐渠道恢复")
    print("-" * 50)
    drill_content = (
        "尊敬的客户，本理财产品（演练测试版本）包含以下披露："
        "风险提示：投资有风险，入市需谨慎。过往业绩不代表未来表现。"
        "投资者适当性：本产品适合中高风险承受能力投资者。"
    )
    drill = mgr.create_drill(
        name="Q2合规话术回滚演练",
        target_version="3.0.0",
        rollback_version="2.1.0",
        channels=["app", "wechat"],
        drill_content=drill_content,
        business_type="理财",
        channel="app",
        operator="合规-赵六",
    )
    print(f"  演练ID: {drill.drill_id}")
    print(f"  演练名称: {drill.name}")
    print(f"  演练状态: {drill.status.value}")
    print(f"  合规校验: {'通过' if drill.compliance_check_passed else '未通过'}")
    if drill.compliance_check_passed:
        print(f"  渠道恢复结果:")
        for r in drill.channel_recovery_results:
            print(f"    - [{r['status']}] {r['channel_name']} -> {r['restored_version']} ({r['detail'][:30]}...)")
    print(f"  总体恢复结果: {drill.recovery_result}")

    print("\n【场景5】稳定话术版本查询")
    print("-" * 50)
    stable = mgr.get_stable_version()
    if not stable:
        print("  暂无稳定版本（暂无自动回滚触发过）")
    else:
        print(f"  当前稳定版本:")
        for s in stable[:5]:
            print(f"    - 渠道{s.get('channel')}/{s.get('business_type')}: 版本{s.get('version')} (设置人: {s.get('set_by')}, 时间: {s.get('set_at', '')[:19]})")

    print("\n【场景6】历史记录查询（全部/按时间范围）")
    print("-" * 50)
    records = mgr.query_history(business_type="理财", limit=10)
    print(f"  查询理财业务记录: {len(records)} 条")
    for r in records[:3]:
        sv = r.get("script_version", {})
        rb = r.get("rollback_report") or {}
        print(f"    {r['publish_id']} | {sv.get('version','')} | {r.get('status','')} | 回滚报告: {rb.get('rollback_id','无')}")

    print("\n【场景7】批量导出（支持时间范围）")
    print("-" * 50)
    start_t = (datetime.now() - timedelta(days=1)).isoformat()
    end_t = (datetime.now() + timedelta(days=1)).isoformat()
    filtered = mgr.query_history(
        publish_time_start=start_t,
        publish_time_end=end_t,
        limit=1000,
    )
    export_path = mgr.batch_export(
        filtered,
        format="csv",
        filename="time_range_export.csv",
        operator="运营-张三",
    )
    print(f"  时间范围: {start_t[:19]} ~ {end_t[:19]}")
    print(f"  匹配记录数: {len(filtered)}")
    print(f"  导出路径: {export_path}")

    print("\n【场景8】周报统计（手动）")
    print("-" * 50)
    report = mgr.generate_weekly_report(operator="系统")
    print(f"  统计周期: {report['stats']['period']}")
    print(f"  发布总数: {report['stats']['total_publishes']}")
    print(f"  发布成功率: {report['stats']['publish_success_rate']}")
    print(f"  回滚次数: {report['stats']['rollback_count']}")
    print(f"  PDF报告: {report['pdf_report']}")
    print(f"  Excel报告: {report['excel_report']}")

    print("\n【场景8.5】周报自动调度（每周一 00:00 触发）")
    print("-" * 50)
    print("  命令: python main.py schedule")
    print("  描述: 启动后常驻运行，到每周一 00:00 自动生成 PDF + Excel 周报")
    print("  提示: 也支持随时手动生成: python main.py report")

    print("\n【场景9】合规日志查询（支持时间范围）")
    print("-" * 50)
    logs = mgr.query_compliance_logs(limit=5)
    print(f"  查询到 {len(logs)} 条日志")
    for log in logs[:3]:
        print(f"    [{log['timestamp'][:19]}] {log['action']} | {log['operator']} | {log['detail'][:40]}")

    print("\n" + "=" * 70)
    print("  演示完成！所有操作已记录到服务合规日志，全程留痕可查。")
    print("  CLI 用法速查:")
    print("    发布:       python main.py publish --version X --content Y ...")
    print("    回滚:       python main.py rollback --publish-id PUB-XXXX --operator 张三")
    print("    演练:       python main.py drill --name 演练1 --target-version 3.0 --rollback-version 2.1 ...")
    print("    周报手动:   python main.py report")
    print("    周报调度:   python main.py schedule")
    print("    稳定版本:   python main.py stable-version --channel app")
    print("    历史查询:   python main.py query --start-time 2026-06-01T00:00:00 --channel app")
    print("    批量导出:   python main.py export --format excel --start-time ...")
    print("    日志查询:   python main.py logs --action auto_rollback_complete --limit 20")
    print("=" * 70)


def run_cli():
    parser = argparse.ArgumentParser(
        description="金融智能客服合规话术发布与服务回滚自动化管理系统"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    pub_parser = subparsers.add_parser("publish", help="提交话术发布申请")
    pub_parser.add_argument("--version", required=True, help="话术版本号")
    pub_parser.add_argument("--content", required=True, help="话术内容")
    pub_parser.add_argument("--business-type", required=True, help="业务类型")
    pub_parser.add_argument("--channel", required=True, choices=["app", "phone", "wechat", "mini_program"], help="服务渠道")
    pub_parser.add_argument("--risk-level", required=True, choices=["routine", "emergency_compliance", "complaint_outbreak"], help="风险级别")
    pub_parser.add_argument("--operator", required=True, help="操作人")

    rollback_parser = subparsers.add_parser("rollback", help="手动回滚")
    rollback_parser.add_argument("--publish-id", required=True, help="发布ID")
    rollback_parser.add_argument("--operator", required=True, help="操作人")
    rollback_parser.add_argument("--reason", default="", help="回滚原因")

    drill_parser = subparsers.add_parser("drill", help="创建回滚演练")
    drill_parser.add_argument("--name", required=True, help="演练名称")
    drill_parser.add_argument("--target-version", required=True, help="目标版本")
    drill_parser.add_argument("--rollback-version", required=True, help="回滚版本")
    drill_parser.add_argument("--channels", nargs="+", default=["app", "phone", "wechat", "mini_program"], help="演练渠道")
    drill_parser.add_argument("--drill-content", default=None, help="演练话术内容（不传则按目标版本或默认模板）")
    drill_parser.add_argument("--business-type", default="理财", help="业务类型，用于按目标版本默认模板")
    drill_parser.add_argument("--channel", default="app", help="服务渠道，用于按目标版本默认模板")
    drill_parser.add_argument("--operator", default="system", help="操作人")

    report_parser = subparsers.add_parser("report", help="生成周报（手动）")
    report_parser.add_argument("--operator", default="system", help="操作人")

    schedule_parser = subparsers.add_parser("schedule", help="启动周报自动调度（每周一0点生成）")
    schedule_parser.add_argument("--operator", default="system", help="操作人")

    stable_parser = subparsers.add_parser("stable-version", help="查询当前稳定话术版本")
    stable_parser.add_argument("--channel", help="按渠道筛选")
    stable_parser.add_argument("--business-type", help="按业务类型筛选")

    query_parser = subparsers.add_parser("query", help="查询历史记录")
    query_parser.add_argument("--start-time", help="发布开始时间 (ISO格式, 如 2026-06-01T00:00:00)")
    query_parser.add_argument("--end-time", help="发布结束时间 (ISO格式, 如 2026-06-30T23:59:59)")
    query_parser.add_argument("--channel", help="服务渠道")
    query_parser.add_argument("--business-type", help="业务类型")
    query_parser.add_argument("--version", help="版本号")
    query_parser.add_argument("--status", help="发布状态")
    query_parser.add_argument("--risk-level", help="风险级别")
    query_parser.add_argument("--operator", help="操作人")
    query_parser.add_argument("--limit", type=int, default=20, help="返回条数")

    export_parser = subparsers.add_parser("export", help="批量导出")
    export_parser.add_argument("--format", choices=["csv", "excel"], default="csv", help="导出格式")
    export_parser.add_argument("--start-time", help="发布开始时间 (ISO格式)")
    export_parser.add_argument("--end-time", help="发布结束时间 (ISO格式)")
    export_parser.add_argument("--channel", help="服务渠道")
    export_parser.add_argument("--business-type", help="业务类型")
    export_parser.add_argument("--version", help="版本号")
    export_parser.add_argument("--status", help="发布状态")
    export_parser.add_argument("--risk-level", help="风险级别")
    export_parser.add_argument("--operator", help="操作人")
    export_parser.add_argument("--filename", default=None, help="导出文件名")

    log_parser = subparsers.add_parser("logs", help="查询合规日志")
    log_parser.add_argument("--action", help="操作类型")
    log_parser.add_argument("--operator", help="操作人")
    log_parser.add_argument("--target", help="目标")
    log_parser.add_argument("--start-time", help="开始时间 (ISO格式)")
    log_parser.add_argument("--end-time", help="结束时间 (ISO格式)")
    log_parser.add_argument("--limit", type=int, default=20, help="返回条数")

    subparsers.add_parser("demo", help="运行演示")

    args = parser.parse_args()
    mgr = ComplianceScriptManager("config.yaml")

    if args.command == "publish":
        import time
        record = mgr.submit_publish_request(
            version=args.version,
            content=args.content,
            business_type=args.business_type,
            channel=args.channel,
            risk_level=args.risk_level,
            operator=args.operator,
        )
        time.sleep(3)
        print(json.dumps({
            "publish_id": record.publish_id,
            "status": record.status.value,
            "pre_check_passed": record.pre_check_result.passed if record.pre_check_result else None,
            "rollback_report_id": record.rollback_report.rollback_id if record.rollback_report else None,
            "restored_version": record.rollback_report.restored_version if record.rollback_report else None,
        }, ensure_ascii=False, indent=2))
        if record.status == PublishStatus.ROLLED_BACK:
            print("  (发布监控异常，已自动回滚)")

    elif args.command == "rollback":
        result = mgr.manual_rollback(args.publish_id, args.operator, args.reason)
        if result:
            print(json.dumps({
                "rollback_id": result.rollback_id,
                "publish_id": result.publish_id,
                "trigger": result.trigger.value,
                "restored_version": result.restored_version,
                "channel_impact": result.channel_impact,
            }, ensure_ascii=False, indent=2))
        else:
            print("回滚失败：未找到对应的发布记录")

    elif args.command == "drill":
        drill = mgr.create_drill(
            name=args.name,
            target_version=args.target_version,
            rollback_version=args.rollback_version,
            channels=args.channels,
            drill_content=args.drill_content,
            business_type=args.business_type,
            channel=args.channel,
            operator=args.operator,
        )
        print(json.dumps({
            "drill_id": drill.drill_id,
            "name": drill.name,
            "status": drill.status.value,
            "compliance_passed": drill.compliance_check_passed,
            "recovery_result": drill.recovery_result,
        }, ensure_ascii=False, indent=2))

    elif args.command == "report":
        report = mgr.generate_weekly_report(args.operator)
        print(json.dumps(report, ensure_ascii=False, indent=2))

    elif args.command == "schedule":
        mgr.schedule_weekly_report(args.operator)

    elif args.command == "stable-version":
        stable = mgr.get_stable_version(args.channel, args.business_type)
        if not stable:
            print("暂无稳定版本记录")
        else:
            print(json.dumps(stable, ensure_ascii=False, indent=2))

    elif args.command == "query":
        records = mgr.query_history(
            publish_time_start=args.start_time,
            publish_time_end=args.end_time,
            channel=args.channel,
            business_type=args.business_type,
            version=args.version,
            status=args.status,
            risk_level=args.risk_level,
            operator=args.operator,
            limit=args.limit,
        )
        print(f"共匹配 {len(records)} 条记录:")
        print(json.dumps(records, ensure_ascii=False, indent=2))

    elif args.command == "export":
        records = mgr.query_history(
            publish_time_start=args.start_time,
            publish_time_end=args.end_time,
            channel=args.channel,
            business_type=args.business_type,
            version=args.version,
            status=args.status,
            risk_level=args.risk_level,
            operator=args.operator,
            limit=10000,
        )
        path = mgr.batch_export(records, format=args.format, filename=args.filename, operator=args.operator or "system")
        print(f"导出完成 ({len(records)} 条): {path}")

    elif args.command == "logs":
        logs = mgr.query_compliance_logs(
            action=args.action,
            operator=args.operator,
            target=args.target,
            start_time=args.start_time,
            end_time=args.end_time,
            limit=args.limit,
        )
        print(json.dumps(logs, ensure_ascii=False, indent=2))

    elif args.command == "demo":
        run_demo()

    else:
        parser.print_help()


if __name__ == "__main__":
    run_cli()
