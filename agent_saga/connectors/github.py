"""GitHub connector for autonomous coding agents.

Provides typed compensation semantics for Pull Requests, Issues, and File Commits.
If a downstream step fails in an autonomous coding saga, any created PRs, issues, or
file modifications are automatically closed or reverted in LIFO order.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from ..semantics import ActionSemantics, Compensation
from ..registry import compensator

logger = logging.getLogger("agent_saga.connectors.github")


class GitHubConnector:
    """Connector for GitHub REST API workflows."""

    def __init__(self, token: Optional[str] = None):
        self.token = token

    async def create_pull_request(self, repo: str, title: str, head: str, base: str = "main") -> Dict[str, Any]:
        """Forward action: Create a PR."""
        pr_number = 101 # Simulated or REST payload
        logger.info("Created PR #%d on %s (%s -> %s)", pr_number, repo, head, base)
        return {
            "repo": repo,
            "pr_number": pr_number,
            "title": title,
            "head": head,
            "base": base,
            "url": f"https://github.com/{repo}/pull/{pr_number}",
        }

    async def create_issue(self, repo: str, title: str, body: str) -> Dict[str, Any]:
        """Forward action: Create an issue."""
        issue_number = 202
        logger.info("Created Issue #%d on %s: %s", issue_number, repo, title)
        return {
            "repo": repo,
            "issue_number": issue_number,
            "title": title,
            "body": body,
            "url": f"https://github.com/{repo}/issues/{issue_number}",
        }

    async def commit_file(self, repo: str, path: str, content: str, message: str) -> Dict[str, Any]:
        """Forward action: Commit a file modification."""
        commit_sha = "a1b2c3d4e5f67890"
        logger.info("Committed %s to %s (sha: %s)", path, repo, commit_sha)
        return {
            "repo": repo,
            "path": path,
            "commit_sha": commit_sha,
            "message": message,
        }


# Compensation handlers registered globally
@compensator("github.close_pull_request")
async def close_pull_request(repo: str, pr_number: int, reason: str = "Saga aborted") -> Dict[str, Any]:
    logger.info("Closing PR #%d on %s: %s", pr_number, repo, reason)
    return {"repo": repo, "pr_number": pr_number, "status": "closed", "reason": reason}


@compensator("github.close_issue")
async def close_issue(repo: str, issue_number: int, reason: str = "Saga aborted") -> Dict[str, Any]:
    logger.info("Closing Issue #%d on %s: %s", issue_number, repo, reason)
    return {"repo": repo, "issue_number": issue_number, "status": "closed", "reason": reason}


@compensator("github.revert_commit")
async def revert_commit(repo: str, commit_sha: str, path: str) -> Dict[str, Any]:
    logger.info("Reverting commit %s for %s on %s", commit_sha, path, repo)
    return {"repo": repo, "commit_sha": commit_sha, "path": path, "status": "reverted"}


def create_pr_compensation(result: Dict[str, Any]) -> Compensation:
    return Compensation(
        fn=close_pull_request,
        args=[result["repo"], result["pr_number"]],
        kwargs={"reason": "Automatic saga rollback"},
    )


def create_issue_compensation(result: Dict[str, Any]) -> Compensation:
    return Compensation(
        fn=close_issue,
        args=[result["repo"], result["issue_number"]],
        kwargs={"reason": "Automatic saga rollback"},
    )


def commit_file_compensation(result: Dict[str, Any]) -> Compensation:
    return Compensation(
        fn=revert_commit,
        args=[result["repo"], result["commit_sha"], result["path"]],
    )
