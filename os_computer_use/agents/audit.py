import logging
import os
import re
from typing import Any, Dict, Optional


class AuditAgent:
    """Audit risky operations and request user authorization when needed."""

    RISKY_KINDS = {
        "filesystem.delete",
        "filesystem.move",
        "filesystem.rename",
        "filesystem.overwrite",
        "browser.send",
        "command.run",
    }

    RISKY_PATH_PATTERNS = [
        r"^/etc/",
        r"^/usr/",
        r"^/bin/",
        r"^/sbin/",
        r"^/boot/",
        r"^/root/",
        r"^/home/[^/]+/\.",
    ]

    def __init__(
        self,
        log_file: str = "audit.log",
        auto_approve: bool = False,
        authorization_callback: Optional[Any] = None,
    ):
        self.log_file = os.path.expanduser(log_file)
        self.auto_approve = auto_approve
        self.authorization_callback = authorization_callback
        self._setup_logging()

    def _setup_logging(self) -> None:
        self.logger = logging.getLogger(f"AuditAgent:{self.log_file}")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            handler = logging.FileHandler(self.log_file, encoding="utf-8")
            formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def log_action(self, action_type: str, details: str, status: str) -> None:
        self.logger.info("Action: %s | Details: %s | Status: %s", action_type, details, status)

    def is_risky(self, kind: str, arguments: Dict[str, Any]) -> bool:
        if kind in self.RISKY_KINDS:
            return True

        for path_key in ["file_path", "src", "path", "dst_dir"]:
            value = arguments.get(path_key, "")
            if not isinstance(value, str):
                continue
            for pattern in self.RISKY_PATH_PATTERNS:
                if re.match(pattern, value):
                    return True
        return False

    def request_authorization(self, action_type: str, details: str) -> bool:
        self.log_action(action_type, details, "WAITING_FOR_CONFIRMATION")

        if self.auto_approve:
            self.log_action(action_type, details, "AUTO_APPROVED")
            return True

        if self.authorization_callback is not None:
            try:
                authorized = bool(self.authorization_callback(action_type, details))
            except Exception:
                authorized = False
            self.log_action(action_type, details, "AUTHORIZED" if authorized else "REJECTED")
            return authorized

        print("\n[Audit] Risky operation requires confirmation")
        print(f"  Action: {action_type}")
        print(f"  Details: {details}")
        try:
            answer = input("  Allow? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        authorized = answer in ("y", "yes")
        self.log_action(action_type, details, "AUTHORIZED" if authorized else "REJECTED")
        if not authorized:
            print("  Rejected")
        else:
            print("  Authorized")
        return authorized
