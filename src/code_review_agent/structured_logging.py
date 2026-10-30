"""JSON logging with credential redaction and zero-code retention sanitization."""

import json
import logging
import os
import re
from datetime import datetime, timezone


class RedactedJSONFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        for key, value in os.environ.items():
            if len(value) >= 8 and any(
                word in key.upper()
                for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
            ):
                message = message.replace(value, "[REDACTED]")
        message = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[REDACTED]", message)
        message = re.sub(
            r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", message
        )
        message = re.sub(
            r"(?i)([?&](?:token|key|secret|password)=)[^&\s]+", r"\1[REDACTED]", message
        )

        # Zero Code Retention (ZDR) log sanitization: redact raw diff hunks and code snippets
        if os.getenv("ZERO_CODE_LOGGING", "true").lower() in ("1", "true", "yes"):
            message = re.sub(
                r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@[\s\S]*?(?=(?:\n\n|\Z))",
                "[DIFF_HUNK_REDACTED_ZDR]",
                message,
            )
            message = re.sub(
                r"```[a-zA-Z]*\n[\s\S]*?```",
                "[CODE_BLOCK_REDACTED_ZDR]",
                message,
            )

        return json.dumps(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            },
            ensure_ascii=True,
        )
