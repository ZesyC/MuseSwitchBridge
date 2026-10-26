"""Saved Muse sessions and quota-driven account selection. No credentials in status."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


class QuotaExhaustedError(RuntimeError):
    """The provider confirmed that an account cannot accept more requests."""


class WeeklyQuotaExhaustedError(QuotaExhaustedError):
    """Weekly allowance ended; additional credits may still be available."""


def account_state_path(base_dir: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("Account names must contain 1–64 letters, digits, underscores or hyphens")
    path = base_dir / "storage_state.json" if name == "default" else base_dir / ".muse_accounts" / f"{name}.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Account session files and directories must not be symlinks")
    return path


def saved_accounts(base_dir: Path) -> list[str]:
    names = ["default"] if account_state_path(base_dir, "default").is_file() else []
    directory = base_dir / ".muse_accounts"
    names.extend(sorted(path.stem for path in directory.glob("*.json") if path.is_file() and not path.is_symlink() and path.stem != "default" and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", path.stem)))
    return names


def parse_subscription_response(text: str) -> dict:
    """Extract only quota fields from Muse's Settings server-action response."""
    for line in text.splitlines():
        try:
            value = json.loads(line.split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        if not isinstance(value, dict) or value.get("success") is not True:
            continue
        subscription = value.get("subscription")
        if not isinstance(subscription, dict) or not isinstance(subscription.get("usage"), dict):
            continue
        usage = subscription["usage"]
        status = usage.get("quotaStatus")
        if not isinstance(status, str):
            continue
        reset = usage.get("resetsAt")
        percent = usage.get("percentUsed")
        balance = subscription.get("topupBalance")
        return {
            "quota_status": status,
            "exhausted": status.upper() in {"EXHAUSTED", "INSUFFICIENT", "DEPLETED", "LIMIT_REACHED", "QUOTA_EXCEEDED"},
            "percent_used": percent if isinstance(percent, (int, float)) and not isinstance(percent, bool) else None,
            "resets_at": reset if isinstance(reset, (int, float)) and not isinstance(reset, bool) else None,
            "plan": subscription.get("usageRowLabel"),
            "usage_label": subscription.get("usageRowValueLabel"),
            "reset_label": subscription.get("statusSubtitle"),
            # topupBalance uses provider units; the display label is the token balance.
            "additional_tokens_label": subscription.get("topupRowValueLabel") if isinstance(subscription.get("topupRowValueLabel"), str) else None,
            "additional_available": balance > 0 if isinstance(balance, (int, float)) and not isinstance(balance, bool) else None,
            "checked_at": time.time(),
        }
    raise ValueError("Muse Settings did not return a subscription quota")


def quota_alert(text: str) -> bool:
    """Only use this on provider alert/toast UI, never on model or user prose."""
    return bool(re.search(
        r"(?:reached|exceeded|used up|hit)\s+(?:your\s+|the\s+)?(?:(?:(?:weekly|usage|token)\s+){1,3}limit|quota)"
        r"|(?:(?:weekly|usage|token)\s+){1,3}(?:limit|quota)\s+(?:reached|exceeded|exhausted)"
        r"|(?:out of|no more)\s+(?:tokens|quota)|quota\s+(?:exhausted|depleted)"
        r"|(?:đã\s+)?(?:hết|vượt)\s+(?:hạn mức|quota|giới hạn sử dụng)",
        text, flags=re.IGNORECASE,
    ))


class AccountPool:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.active: str | None = None
        self.phase = "weekly"
        self.quotas: dict[str, dict[str, Any]] = {}

    def available(self, name: str, now: float | None = None) -> bool:
        quota = self.quotas.get(name, {})
        if not quota.get("exhausted"):
            return True
        reset = quota.get("resets_at")
        return isinstance(reset, (int, float)) and reset <= (time.time() if now is None else now)

    def weekly_available(self, name: str) -> bool | None:
        quota = self.quotas.get(name, {})
        reset = quota.get("resets_at")
        if quota.get("error") or (isinstance(reset, (int, float)) and quota.get("checked_at", 0) < reset <= time.time()):
            return None
        if quota.get("exhausted"):
            return False
        percent = quota.get("percent_used")
        return percent < 100 if isinstance(percent, (int, float)) and not isinstance(percent, bool) else None

    def select(self, tried: set[str] | None = None) -> str:
        tried = tried or set()
        names = saved_accounts(self.base_dir)
        if not names:
            raise RuntimeError("No saved Muse accounts; run 'python muse_bridge.py login'")
        ordered = list(names)
        if self.phase == "weekly" and self.active in ordered:
            ordered.remove(self.active)
            ordered.insert(0, self.active)
        for name in ordered:
            if name not in tried and self.weekly_available(name) is True:
                self.active, self.phase = name, "weekly"
                return name
        if any(self.weekly_available(name) is None for name in names if name not in tried):
            raise RuntimeError("Weekly quota unavailable for some accounts; refusing to spend additional tokens before all weekly quotas are confirmed exhausted")
        if self.phase == "additional" and self.active in names:
            names.remove(self.active)
            names.insert(0, self.active)
        for name in names:
            quota = self.quotas.get(name, {})
            if name not in tried and not quota.get("exhausted") and quota.get("additional_available") is True:
                self.active, self.phase = name, "additional"
                return name
        if any(not self.quotas.get(name, {}).get("exhausted") and self.quotas.get(name, {}).get("additional_available") is None for name in names if name not in tried):
            raise RuntimeError("Additional token balance unavailable; run 'python muse_bridge.py quota --all'")
        raise QuotaExhaustedError("All saved Muse accounts are out of quota; run 'python muse_bridge.py quota --all' to check reset times")

    def exhausted(self, name: str, *, weekly_only: bool = False) -> None:
        quota = {**self.quotas.get(name, {}), "percent_used": 100, "checked_at": time.time()}
        quota.pop("error", None)
        if not weekly_only:
            quota.update(exhausted=True, quota_status="EXHAUSTED", additional_available=False)
        self.quotas[name] = quota

    def status(self) -> dict:
        return {"active_account": self.active, "quota_phase": self.phase, "accounts": [
            {"name": name, "active": name == self.active, "quota": self.quotas.get(name)}
            for name in saved_accounts(self.base_dir)
        ]}
