import uuid
from datetime import datetime
from typing import Optional
from models import (
    RiskLevel,
    ApprovalFlow,
    ApprovalNode,
    ApprovalStatus,
    PublishRecord,
)
from compliance_log import ComplianceLogger


class ApprovalManager:
    APPROVER_POOL = {
        "运营主管": ["张运营", "李运营"],
        "合规专员": ["王合规", "赵合规"],
        "客服主管": ["刘客服", "陈客服"],
        "法务专员": ["周法务", "吴法务"],
    }

    def __init__(self, config: dict, logger: ComplianceLogger):
        self.config = config.get("approval", {})
        self.risk_levels = self.config.get("risk_levels", {})
        self.logger = logger

    def create_approval_flow(
        self, risk_level: RiskLevel, operator: str = "system"
    ) -> ApprovalFlow:
        flow = ApprovalFlow(risk_level=risk_level)
        risk_cfg = self.risk_levels.get(risk_level.value, {})
        approvers_cfg = risk_cfg.get("approvers", [])

        for approver_cfg in approvers_cfg:
            role = approver_cfg["role"]
            required = approver_cfg.get("required", True)
            assignees = self.APPROVER_POOL.get(role, [])
            approver_name = assignees[0] if assignees else role
            node = ApprovalNode(
                role=role,
                required=required,
                status=ApprovalStatus.PENDING,
                approver=approver_name,
            )
            flow.nodes.append(node)

        self.logger.log(
            "approval_flow_created",
            operator,
            risk_level.value,
            f"创建审批流程，共{len(flow.nodes)}个审批节点",
            {
                "risk_level": risk_level.value,
                "risk_name": risk_cfg.get("name", ""),
                "nodes": [
                    {"role": n.role, "approver": n.approver, "required": n.required}
                    for n in flow.nodes
                ],
            },
        )
        return flow

    def approve_node(
        self,
        flow: ApprovalFlow,
        role: str,
        approver: str,
        comment: str = "",
        operator: str = "system",
    ) -> bool:
        for node in flow.nodes:
            if node.role == role and node.status == ApprovalStatus.PENDING:
                node.status = ApprovalStatus.APPROVED
                node.approver = approver
                node.comment = comment
                node.approved_at = datetime.now()
                self.logger.log(
                    "approval_node_approved",
                    operator,
                    role,
                    f"审批节点通过: {role} - {approver}",
                    {"comment": comment},
                )
                return True
        return False

    def reject_node(
        self,
        flow: ApprovalFlow,
        role: str,
        approver: str,
        comment: str = "",
        operator: str = "system",
    ) -> bool:
        for node in flow.nodes:
            if node.role == role and node.status == ApprovalStatus.PENDING:
                node.status = ApprovalStatus.REJECTED
                node.approver = approver
                node.comment = comment
                node.approved_at = datetime.now()
                flow.status = ApprovalStatus.REJECTED
                self.logger.log(
                    "approval_node_rejected",
                    operator,
                    role,
                    f"审批节点拒绝: {role} - {approver}",
                    {"comment": comment},
                )
                return True
        return False

    def is_flow_approved(self, flow: ApprovalFlow) -> bool:
        for node in flow.nodes:
            if node.required and node.status != ApprovalStatus.APPROVED:
                return False
        flow.status = ApprovalStatus.APPROVED
        return True

    def is_flow_rejected(self, flow: ApprovalFlow) -> bool:
        return flow.status == ApprovalStatus.REJECTED

    def get_pending_nodes(self, flow: ApprovalFlow) -> list:
        return [n for n in flow.nodes if n.status == ApprovalStatus.PENDING]

    def get_flow_summary(self, flow: ApprovalFlow) -> dict:
        return {
            "risk_level": flow.risk_level.value,
            "status": flow.status.value,
            "total_nodes": len(flow.nodes),
            "approved_nodes": sum(
                1 for n in flow.nodes if n.status == ApprovalStatus.APPROVED
            ),
            "rejected_nodes": sum(
                1 for n in flow.nodes if n.status == ApprovalStatus.REJECTED
            ),
            "pending_nodes": sum(
                1 for n in flow.nodes if n.status == ApprovalStatus.PENDING
            ),
            "nodes": [
                {
                    "role": n.role,
                    "approver": n.approver,
                    "status": n.status.value,
                    "comment": n.comment,
                    "approved_at": n.approved_at.isoformat() if n.approved_at else None,
                }
                for n in flow.nodes
            ],
        }
