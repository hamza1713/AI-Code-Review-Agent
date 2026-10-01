"""Durable write intent. Uncertain writes are reconciled, never blindly replayed."""

import hashlib
import json
import os
import time


class ReconciliationRequired(RuntimeError):
    """Raised when a write intent has an uncertain outcome and must be reconciled."""


def valid_receipt(receipt, receipt_key="id"):
    if not isinstance(receipt, dict) or receipt.get("error"):
        return False
    value = receipt.get(receipt_key)
    return (type(value) is int and value > 0) or (
        type(value) is str and bool(value.strip())
    )


def publish(
    queue,
    *,
    job_id,
    claim_token,
    key,
    target,
    payload,
    send,
    search=None,
    receipt_key="id",
):
    marker = f"<!-- review-operation:{hashlib.sha256(key.encode()).hexdigest()} -->"
    queue.assert_claim(job_id, claim_token)
    existing = queue.get_external_operation(key)
    if existing and existing["target_id"] != target:
        raise ReconciliationRequired(
            "RECONCILIATION_REQUIRED: operation target mismatch"
        )
    with queue._get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        queue.assert_claim(job_id, claim_token)
        conn.execute(
            "INSERT OR IGNORE INTO external_operation_jobs VALUES (?,?)", (key, job_id)
        )
    if existing and existing["status"] == "SUCCESS":
        return (
            (existing.get("result") or {}).get("receipt")
            or existing.get("result")
            or {}
        )
    if existing:
        # Retain the first intent even if analysis produces different text on retry.
        payload = (existing.get("result") or {}).get("intent", payload)
        try:
            if search is None:
                raise ReconciliationRequired(
                    "Provider lookup is unavailable for this write"
                )
            login = os.environ["BOT_GITHUB_LOGIN"].lower()
            matches = [
                r
                for r in search()
                if marker in (r.get("body") or "")
                and (r.get("user") or {}).get("login", "").lower() == login
            ]
        except Exception as error:
            raise ReconciliationRequired(
                "RECONCILIATION_REQUIRED: provider lookup failed"
            ) from error
        if len(matches) != 1:
            raise ReconciliationRequired(
                "RECONCILIATION_REQUIRED: delivery could not be determined"
            )
        receipt = matches[0]
        if not valid_receipt(receipt, receipt_key):
            raise ReconciliationRequired(
                "RECONCILIATION_REQUIRED: provider lookup returned no receipt"
            )
    else:
        with queue._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            queue.assert_claim(job_id, claim_token)
            inserted = conn.execute(
                """INSERT OR IGNORE INTO external_operations_log
                (operation_key,operation_type,target_id,payload_hash,status,result_json,created_at,updated_at)
                VALUES (?,?,?,?,'PENDING',?,?,?)""",
                (
                    key,
                    "publication",
                    target,
                    hashlib.sha256(
                        json.dumps(payload, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                    json.dumps(
                        {
                            "intent": payload,
                            "marker": marker,
                            "receipt_key": receipt_key,
                        },
                        default=str,
                    ),
                    time.time(),
                    time.time(),
                ),
            ).rowcount
        if not inserted:
            raise ReconciliationRequired(
                "RECONCILIATION_REQUIRED: concurrent publication intent"
            )
        queue.assert_claim(job_id, claim_token)
        try:
            receipt = send(marker)
            if not valid_receipt(receipt, receipt_key):
                raise RuntimeError("Provider did not acknowledge publication")
        except Exception as error:
            if isinstance(error, ReconciliationRequired):
                raise
            raise ReconciliationRequired(
                "RECONCILIATION_REQUIRED: provider write has uncertain outcome"
            ) from error
    with queue._get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        queue.assert_claim(job_id, claim_token)
        conn.execute(
            """UPDATE external_operations_log SET status='SUCCESS',result_json=?,updated_at=?
            WHERE operation_key=?""",
            (
                json.dumps(
                    {"receipt": receipt, "intent": payload, "receipt_key": receipt_key},
                    default=str,
                ),
                time.time(),
                key,
            ),
        )
    return receipt
