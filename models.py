from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import Optional


class RiskLevel(Enum):
    ROUTINE = "routine"
    EMERGENCY_COMPLIANCE = "emergency_compliance"
    COMPLAINT_OUTBREAK = "complaint_outbreak"


class Channel(Enum):
    APP = "app"
    PHONE = "phone"
    WECHAT = "wechat"
    MINI_PROGRAM = "mini_program"


class PublishStatus(Enum):
    PENDING_CHECK = "pending_check"
    CHECK_FAILED = "check_failed"
    PENDING_APPROVAL = "pending_approval"
    APPROVAL_REJECTED = "approval_rejected"
    GRAY_RELEASING = "gray_releasing"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class MonitorMetricType(Enum):
    RESPONSE_RATE = "response_rate"
    VIOLATION_RATE = "violation_rate"
    COMPLAINT_RATE = "complaint_rate"
    SERVICE_INTERRUPTION = "service_interruption"


class RollbackTrigger(Enum):
    AUTO = "auto"
    MANUAL = "manual"
    DRILL = "drill"


class RollbackApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MANUAL_REQUIRED = "manual_required"


class DrillStatus(Enum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ScriptVersion:
    version: str
    content: str
    business_type: str
    channel: str
    created_at: datetime = field(default_factory=datetime.now)
    checksum: str = ""


@dataclass
class PreCheckResult:
    passed: bool
    compliance_ok: bool = True
    compliance_issues: list = field(default_factory=list)
    regulatory_ok: bool = True
    regulatory_issues: list = field(default_factory=list)
    info_protection_ok: bool = True
    info_protection_issues: list = field(default_factory=list)
    service_available: bool = True
    service_issues: list = field(default_factory=list)
    checked_at: datetime = field(default_factory=datetime.now)


@dataclass
class ApprovalNode:
    role: str
    required: bool
    status: ApprovalStatus = ApprovalStatus.PENDING
    approver: str = ""
    comment: str = ""
    approved_at: Optional[datetime] = None


@dataclass
class ApprovalFlow:
    risk_level: RiskLevel
    nodes: list = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    status: ApprovalStatus = ApprovalStatus.PENDING


@dataclass
class GrayReleaseStage:
    channel: Channel
    ratio: float
    duration_minutes: int
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class MonitorSnapshot:
    timestamp: datetime = field(default_factory=datetime.now)
    response_rate: float = 0.0
    violation_rate: float = 0.0
    complaint_rate: float = 0.0
    service_interruption: int = 0
    channel: str = ""


@dataclass
class RollbackApprovalNode:
    role: str
    required: bool
    status: RollbackApprovalStatus = RollbackApprovalStatus.PENDING
    approver: str = ""
    comment: str = ""
    approved_at: Optional[datetime] = None
    notified_at: Optional[datetime] = None


@dataclass
class RollbackNotificationTrack:
    role: str
    status: str = "pending"
    notified_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None
    acked_by: str = ""
    detail: str = ""


@dataclass
class RollbackReport:
    rollback_id: str
    publish_id: str
    trigger: RollbackTrigger
    channel_impact: list = field(default_factory=list)
    violation_reasons: list = field(default_factory=list)
    complaint_stats: dict = field(default_factory=dict)
    rolled_back_at: datetime = field(default_factory=datetime.now)
    restored_version: str = ""
    notified_roles: list = field(default_factory=list)
    approval_status: RollbackApprovalStatus = RollbackApprovalStatus.APPROVED
    approval_nodes: list = field(default_factory=list)
    notification_tracking: list = field(default_factory=list)
    needs_manual: bool = False
    manual_reason: str = ""


@dataclass
class DrillPlan:
    drill_id: str
    name: str
    target_version: str
    rollback_version: str
    channels: list = field(default_factory=list)
    planned_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: DrillStatus = DrillStatus.PLANNED
    compliance_check_passed: bool = False
    compliance_issues: list = field(default_factory=list)
    channel_recovery_results: list = field(default_factory=list)
    recovery_result: str = ""


@dataclass
class PublishRecord:
    publish_id: str
    script_version: ScriptVersion
    risk_level: RiskLevel
    status: PublishStatus
    operator: str
    submitted_at: datetime = field(default_factory=datetime.now)
    pre_check_result: Optional[PreCheckResult] = None
    approval_flow: Optional[ApprovalFlow] = None
    gray_stages: list = field(default_factory=list)
    monitor_snapshots: list = field(default_factory=list)
    rollback_report: Optional[RollbackReport] = None
    published_at: Optional[datetime] = None
    rolled_back_at: Optional[datetime] = None


@dataclass
class WeeklyStats:
    period_start: datetime
    period_end: datetime
    total_publishes: int = 0
    success_publishes: int = 0
    publish_success_rate: float = 0.0
    rollback_count: int = 0
    avg_customer_satisfaction: float = 0.0
    by_channel: dict = field(default_factory=dict)
    by_risk_level: dict = field(default_factory=dict)
    by_business_type: dict = field(default_factory=dict)


@dataclass
class ComplianceLogEntry:
    log_id: str
    action: str
    operator: str
    target: str
    detail: str
    timestamp: datetime = field(default_factory=datetime.now)
    extra: dict = field(default_factory=dict)
