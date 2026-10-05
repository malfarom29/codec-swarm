"""Jira intake: the orchestrator talks to Jira Cloud's REST API with an API token (agents use the Atlassian MCP).

Until Jira is connected the board shows EXAMPLE_TICKETS, clearly marked as examples.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

SEARCH_FIELDS = ["summary", "status", "priority", "reporter", "description", "updated"]


class Ticket(BaseModel, frozen=True):
    key: str
    summary: str
    status: str = ""
    priority: str = ""
    reporter: str = ""
    description: str = ""
    url: str = ""
    example: bool = False  # True for the mock tickets shown before Jira is connected


EXAMPLE_TICKETS = [
    Ticket(key="CODEC-901", summary="Add sum_amounts to total a list of amount strings", status="To Do", priority="High", reporter="Example",
           description="sum_amounts(['1.50', '2.25']) == 375; blank strings are skipped; an invalid entry raises ValueError naming its position.",
           example=True),
    Ticket(key="CODEC-902", summary="Add a Money value type that only adds amounts in the same currency", status="To Do", priority="Medium",
           reporter="Example", description="Money(100, 'USD') + Money(250, 'USD') == Money(350, 'USD'); mixing currencies raises ValueError.",
           example=True),
    Ticket(key="CODEC-903", summary="Add compute_tax with banker's rounding from basis points", status="In Progress", priority="Medium",
           reporter="Example", description="compute_tax(10000, 825) == 825; half cents round to even.", example=True),
]


def adf_text(node: Any) -> str:
    """Plain text from Atlassian Document Format, enough for a ticket description."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    inner = adf_text(node.get("content"))
    return f"{inner}\n" if kind in ("paragraph", "heading", "listItem", "codeBlock", "blockquote") else inner


class JiraError(RuntimeError):
    """Jira refused the request; the message says what to fix."""


class JiraClient:
    def __init__(self, site: str, email: str, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self.site = site.rstrip("/")
        self._http = httpx.Client(base_url=self.site, auth=(email, token), timeout=20.0, transport=transport, headers={"Accept": "application/json"})

    def _check(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 401:
            raise JiraError("Jira rejected the email or API token (401). Create a token at id.atlassian.com → Security → API tokens.")
        if response.status_code == 403:
            raise JiraError("This account cannot use that Jira site or project (403).")
        if response.status_code >= 400:
            raise JiraError(f"Jira answered {response.status_code}: {response.text[:200]}")
        return response.json()

    def myself(self) -> str:
        """The connected account's display name: a cheap check that the credentials work."""
        return self._check(self._http.get("/rest/api/3/myself")).get("displayName", "")

    def search(self, jql: str, limit: int = 50) -> list[Ticket]:
        body = {"jql": jql, "fields": SEARCH_FIELDS, "maxResults": limit}
        data = self._check(self._http.post("/rest/api/3/search/jql", json=body))
        tickets = []
        for issue in data.get("issues", []):
            f = issue.get("fields", {})
            tickets.append(Ticket(
                key=issue["key"], summary=f.get("summary") or "", status=(f.get("status") or {}).get("name", ""),
                priority=(f.get("priority") or {}).get("name", ""), reporter=(f.get("reporter") or {}).get("displayName", ""),
                description=adf_text(f.get("description")).strip(), url=f"{self.site}/browse/{issue['key']}",
            ))
        return tickets
