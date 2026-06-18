import re
import json
import os
import uuid
import hashlib
from datetime import datetime
from typing import Optional
from models import PreCheckResult, ScriptVersion
from compliance_log import ComplianceLogger


class PreChecker:
    def __init__(self, config: dict, logger: ComplianceLogger):
        self.config = config.get("pre_check", {})
        self.logger = logger
        self.compliance_cfg = self.config.get("compliance", {})
        self.protection_cfg = self.config.get("info_protection", {})
        self.availability_cfg = self.config.get("service_availability", {})
        self._load_regulatory_rules()

    def _load_regulatory_rules(self):
        rules_file = os.path.join(
            self.config.get("system", {}).get("data_dir", "./data"),
            "regulatory_rules.json",
        )
        self.regulatory_rules = self.compliance_cfg.get("regulatory_rules", [])
        if os.path.exists(rules_file):
            with open(rules_file, "r", encoding="utf-8") as f:
                custom = json.load(f)
                self.regulatory_rules.extend(custom)

    def check(self, script_version: ScriptVersion, operator: str = "system") -> PreCheckResult:
        self.logger.log("pre_check_start", operator, script_version.version, "开始前置条件检查")

        result = PreCheckResult(passed=False)

        result.compliance_ok, result.compliance_issues = self._check_compliance(
            script_version.content
        )
        self.logger.log(
            "compliance_check",
            operator,
            script_version.version,
            f"合规校验结果: {'通过' if result.compliance_ok else '不通过'}",
            {"issues": result.compliance_issues},
        )

        result.regulatory_ok, result.regulatory_issues = self._check_regulatory(
            script_version.content, script_version.business_type
        )
        self.logger.log(
            "regulatory_check",
            operator,
            script_version.version,
            f"监管条款适配: {'通过' if result.regulatory_ok else '不通过'}",
            {"issues": result.regulatory_issues},
        )

        result.info_protection_ok, result.info_protection_issues = self._check_info_protection(
            script_version.content
        )
        self.logger.log(
            "info_protection_check",
            operator,
            script_version.version,
            f"客户信息保护: {'通过' if result.info_protection_ok else '不通过'}",
            {"issues": result.info_protection_issues},
        )

        result.service_available, result.service_issues = self._check_service_availability()
        self.logger.log(
            "service_availability_check",
            operator,
            script_version.version,
            f"服务可用性: {'通过' if result.service_available else '不通过'}",
            {"issues": result.service_issues},
        )

        result.passed = (
            result.compliance_ok
            and result.regulatory_ok
            and result.info_protection_ok
            and result.service_available
        )
        result.checked_at = datetime.now()

        self.logger.log(
            "pre_check_complete",
            operator,
            script_version.version,
            f"前置条件检查完成: {'通过' if result.passed else '不通过'}",
            {
                "compliance_ok": result.compliance_ok,
                "regulatory_ok": result.regulatory_ok,
                "info_protection_ok": result.info_protection_ok,
                "service_available": result.service_available,
            },
        )
        return result

    def _check_compliance(self, content: str) -> tuple:
        if not self.compliance_cfg.get("enabled", True):
            return True, []
        issues = []
        keywords = self.compliance_cfg.get("sensitive_keywords", [])
        for kw in keywords:
            if kw in content:
                issues.append(f"检测到敏感词汇: '{kw}'，违反金融营销宣传规范")
        forbidden_patterns = [
            (r"年化收益率\s*[\d.]+%\s*(以上|以上|不低于)", "暗示保本保收益"),
            (r"无.{0,4}风险", "暗示零风险"),
            (r"稳赚", "承诺收益"),
        ]
        for pattern, desc in forbidden_patterns:
            if re.search(pattern, content):
                issues.append(f"合规风险: {desc} (匹配模式: {pattern})")
        return len(issues) == 0, issues

    def _check_regulatory(self, content: str, business_type: str) -> tuple:
        issues = []
        required_disclosures = {
            "理财": ["风险提示", "投资者适当性"],
            "贷款": ["综合年化利率", "逾期后果"],
            "保险": ["免责条款", "犹豫期"],
            "信用卡": ["年费标准", "计息规则"],
        }
        applicable = required_disclosures.get(business_type, [])
        for disclosure in applicable:
            if disclosure not in content:
                issues.append(
                    f"业务类型'{business_type}'缺少必要披露项: '{disclosure}'"
                )
        for rule in self.regulatory_rules:
            if rule in content or self._check_rule_relevance(content, rule, business_type):
                continue
        return len(issues) == 0, issues

    def _check_rule_relevance(self, content: str, rule: str, business_type: str) -> bool:
        return False

    def _check_info_protection(self, content: str) -> tuple:
        if not self.protection_cfg.get("enabled", True):
            return True, []
        issues = []
        pii_patterns = self.protection_cfg.get("pii_patterns", [])
        pii_names = {
            r"\b\d{17}[\dXx]\b": "身份证号",
            r"\b1[3-9]\d{9}\b": "手机号码",
            r"\b[\w.-]+@[\w.-]+\.\w+\b": "电子邮箱",
            r"\b\d{6,19}\b": "银行卡号",
        }
        for pattern in pii_patterns:
            matches = re.findall(pattern, content)
            if matches:
                name = pii_names.get(pattern, "个人敏感信息")
                issues.append(f"检测到{name}泄露风险，共{len(matches)}处匹配")
        if "客户信息" in content and ("共享" in content or "出售" in content):
            issues.append("话术涉及客户信息共享/出售，违反个人信息保护法")
        return len(issues) == 0, issues

    def _check_service_availability(self) -> tuple:
        issues = []
        timeout = self.availability_cfg.get("check_timeout", 30)
        endpoints = self.availability_cfg.get("endpoints", {})
        if not endpoints:
            return True, []
        import urllib.request
        import urllib.error

        for service_name, url in endpoints.items():
            try:
                req = urllib.request.Request(url, method="GET")
                urllib.request.urlopen(req, timeout=min(timeout, 3))
            except Exception as e:
                issues.append(f"{service_name}服务不可用: {url}, 错误: {str(e)}")
        return len(issues) == 0, issues

    @staticmethod
    def compute_checksum(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
