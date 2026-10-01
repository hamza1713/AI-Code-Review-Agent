"""Budgeted GitHub reaction snapshots; errors never delete existing observations."""

import json
import os
import threading
import time
from urllib.parse import urlsplit

import httpx

from code_review_agent.access_policy import validate_target
from code_review_agent.calibration.persistence import FindingOutcomeStore
from code_review_agent.github_client import GitHubClient


class PollBudgetExhausted(RuntimeError):
    pass


class ReactionPoller:
    def __init__(self, store=None, budget=100, interval=300):
        self.store = store or FindingOutcomeStore()
        self.budget = budget
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = None

    def state(self, key, default=None):
        row = (
            self.store._get_connection()
            .execute("SELECT value FROM reaction_poll_state WHERE key=?", (key,))
            .fetchone()
        )
        return json.loads(row[0]) if row else default

    def save(self, key, value):
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO reaction_poll_state VALUES (?,?)",
                (key, json.dumps(value)),
            )

    def cycle(self, transport=None):
        now = time.time()
        if self.state("retry_at", 0) > now:
            return
        rows = [
            dict(r)
            for r in self.store._get_connection().execute(
                """SELECT * FROM finding_outcomes
            WHERE provider='github' AND publication_status='PUBLISHED' AND provider_comment_id<>''
            AND (pr_closed_at IS NULL OR pr_closed_at>=?) ORDER BY finding_id""",
                (now - 30 * 86400,),
            )
        ]
        cursor = self.state("cursor", "")
        rows = [r for r in rows if r["finding_id"] > cursor] + [
            r for r in rows if r["finding_id"] <= cursor
        ]
        remaining = self.budget
        github = GitHubClient()
        bot = os.environ.get("BOT_GITHUB_LOGIN", "").lower()
        if not bot:
            self.save("error", "BOT_GITHUB_LOGIN is required for reaction polling")
            return
        roles, metadata = {}, {}
        with httpx.Client(
            headers=github._get_headers(), timeout=15, transport=transport
        ) as client:

            def get(path, headers=None):
                nonlocal remaining
                if remaining <= 0 or self.state("retry_at", 0) > time.time():
                    raise PollBudgetExhausted()
                url = path if path.startswith("https://") else github.base_url + path
                if urlsplit(url).netloc != urlsplit(github.base_url).netloc:
                    raise ValueError("Untrusted provider pagination URL")
                remaining -= 1
                response = client.get(url, headers=headers)
                if (
                    response.status_code in (403, 429)
                    or response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    retry = max(
                        float(response.headers.get("x-ratelimit-reset", now + 300)),
                        now + float(response.headers.get("retry-after", "300")),
                    )
                    self.save("retry_at", retry)
                    if response.status_code != 200:
                        raise PollBudgetExhausted()
                if response.status_code != 304:
                    response.raise_for_status()
                return response

            for row in rows:
                fid, repo, number, cid = (
                    row["finding_id"],
                    row["repo_id"],
                    row["pr_id"],
                    row["provider_comment_id"],
                )
                try:
                    validate_target("github", row["host"], repo)
                    if row["host"] != "github.com":
                        raise ValueError("Unsupported polling host")
                    pr_key = (repo, number)
                    if pr_key not in metadata:
                        metadata[pr_key] = get(f"/repos/{repo}/pulls/{number}").json()
                    pr = metadata[pr_key]
                    if pr.get("state") == "closed":
                        from datetime import datetime

                        closed = datetime.fromisoformat(
                            pr["closed_at"].replace("Z", "+00:00")
                        ).timestamp()
                        with self.store.transaction() as conn:
                            conn.execute(
                                "UPDATE finding_outcomes SET pr_closed_at=? WHERE provider=? AND host=? AND repo_id=? AND pr_id=?",
                                (closed, "github", row["host"], repo, number),
                            )
                        if closed < now - 30 * 86400:
                            self.save("cursor", fid)
                            continue
                    comment = get(f"/repos/{repo}/pulls/comments/{cid}").json()
                    if (comment.get("user") or {}).get("login", "").lower() != bot:
                        raise ValueError("Recorded comment does not belong to this bot")
                    etag = self.state("etag:" + fid)
                    url = f"/repos/{repo}/pulls/comments/{cid}/reactions?per_page=100"
                    response = get(url, {"If-None-Match": etag} if etag else None)
                    if response.status_code == 304:
                        self.save("success:" + fid, now)
                        self.save("cursor", fid)
                        continue
                    new_etag = response.headers.get("etag")
                    snapshot = []
                    visited = set()
                    while True:
                        snapshot.extend(response.json())
                        next_url = response.links.get("next", {}).get("url")
                        if not next_url:
                            break
                        if next_url in visited:
                            raise ValueError("Cyclic provider pagination")
                        visited.add(next_url)
                        response = get(next_url)
                    authorized = []
                    for reaction in snapshot:
                        actor = (reaction.get("user") or {}).get("login", "")
                        if (
                            reaction.get("content") not in ("+1", "-1")
                            or not actor
                            or actor.endswith("[bot]")
                        ):
                            continue
                        role_key = (repo, actor)
                        if role_key not in roles:
                            from urllib.parse import quote

                            permission = get(
                                f"/repos/{repo}/collaborators/{quote(actor, safe='')}/permission"
                            ).json()
                            roles[role_key] = permission.get(
                                "role_name"
                            ) or permission.get("permission")
                        if roles[role_key] in ("write", "maintain", "admin"):
                            authorized.append(
                                {
                                    "id": str(reaction["id"]),
                                    "actor": actor,
                                    "content": reaction["content"],
                                }
                            )
                    self.store.reconcile_reactions(fid, authorized, host=row["host"])
                    self.save("etag:" + fid, new_etag)
                    self.save("success:" + fid, now)
                    self.save("cursor", fid)
                except PollBudgetExhausted:
                    break
                except Exception as error:
                    self.save("error", type(error).__name__)
                    self.save("cursor", fid)
        self.save("last_cycle", now)
        self.save("requests_used", self.budget - remaining)

    def start(self):
        def loop():
            while not self.stopped.is_set():
                try:
                    self.cycle()
                except Exception as error:
                    from code_review_agent.config import logger

                    logger.error(
                        "Reaction polling cycle failed: %s", type(error).__name__
                    )
                self.stopped.wait(self.interval)

        self.stopped.clear()
        self.thread = threading.Thread(target=loop, name="ReactionPoller", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=5)
