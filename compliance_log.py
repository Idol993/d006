import json
import os
import uuid
from datetime import datetime
from typing import Optional


class ComplianceLogger:
    def __init__(self, config: dict):
        self.log_dir = config.get("system", {}).get("log_dir", "./logs")
        self.retention_days = config.get("logging", {}).get("retention_days", 90)
        self.audit_trail = config.get("logging", {}).get("audit_trail", True)
        self.level = config.get("logging", {}).get("level", "INFO")
        os.makedirs(self.log_dir, exist_ok=True)

    def _get_log_file(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"compliance_{today}.jsonl")

    def log(
        self,
        action: str,
        operator: str,
        target: str,
        detail: str,
        extra: Optional[dict] = None,
    ) -> dict:
        entry = {
            "log_id": str(uuid.uuid4()),
            "action": action,
            "operator": operator,
            "target": target,
            "detail": detail,
            "timestamp": datetime.now().isoformat(),
            "extra": extra or {},
        }
        self._write(entry)
        return entry

    def _write(self, entry: dict):
        log_file = self._get_log_file()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def query(
        self,
        action: Optional[str] = None,
        operator: Optional[str] = None,
        target: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> list:
        results = []
        for filename in sorted(os.listdir(self.log_dir), reverse=True):
            if not filename.startswith("compliance_") or not filename.endswith(".jsonl"):
                continue
            filepath = os.path.join(self.log_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not self._match(entry, action, operator, target, start_time, end_time):
                        continue
                    results.append(entry)
                    if len(results) >= limit:
                        return results
        return results

    def _match(self, entry, action, operator, target, start_time, end_time) -> bool:
        if action and entry.get("action") != action:
            return False
        if operator and entry.get("operator") != operator:
            return False
        if target and entry.get("target") != target:
            return False
        if start_time or end_time:
            ts = datetime.fromisoformat(entry.get("timestamp", ""))
            if start_time and ts < start_time:
                return False
            if end_time and ts > end_time:
                return False
        return True

    def cleanup_expired(self):
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=self.retention_days)
        for filename in os.listdir(self.log_dir):
            if not filename.startswith("compliance_") or not filename.endswith(".jsonl"):
                continue
            date_str = filename.replace("compliance_", "").replace(".jsonl", "")
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            if file_date < cutoff:
                os.remove(os.path.join(self.log_dir, filename))
