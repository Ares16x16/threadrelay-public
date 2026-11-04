"""Explicit audience-aware review over the one durable Outbox approval authority."""
from dataclasses import dataclass
from uuid import uuid4
import json

from threadrelay.storage.outbox import Outbox
from .policy import RoutingDenied


@dataclass(frozen=True)
class ReviewDisclosure:
    """A UI acknowledgment must echo this complete current disclosure, not true.

    This is an internal backend value, not a new renderer command or a claim
    that the future UI has already implemented its confirmation flow.
    """
    plan_id: str
    destination_version: int
    destination_id: str
    title: str
    audience: str
    source_visibility: str
    sender_kind: str
    sender_id: str


class ApprovalStore:
    def __init__(self, router):
        self.router = router
        self.db = router.db

    @staticmethod
    def _disclosure(proposal, current, destination):
        return ReviewDisclosure(proposal["plan_id"], proposal["destination_version"], destination["chat_id"],
            destination["title"], destination["audience"], current["visibility"],
            destination["sender"]["kind"], destination["sender"]["id"])

    def disclosure(self, proposal):
        with self.db.transaction() as con:
            _, current, destination = self.router._proposal(con, proposal)
            return self._disclosure(proposal, current, destination)

    def approve(self, proposal, *, disclosure=None):
        """May stage review while Paused/Stopped; never starts or enqueues a send.

        One approval per contributing route is stored via Outbox.save_approval;
        return the primary route's Approval DTO. Context rows are provenance only,
        not a parallel validity flag or competing approval authority.
        """
        with self.db.transaction() as con:
            receipt, current, destination = self.router._proposal(con, proposal)
            if receipt["mode"] == "dry_run" or not receipt["approval_required"]:
                raise RoutingDenied("approval_not_applicable")
            if not isinstance(disclosure, ReviewDisclosure) or disclosure != self._disclosure(proposal, current, destination):
                raise RoutingDenied("explicit_current_audience_disclosure_required")
            saved = []
            for route in receipt["routes"]:
                existing = [json.loads(row[0]) for row in con.execute("SELECT a.payload FROM approvals a JOIN routing_approval_context c ON c.approval_id=a.id WHERE c.plan_id=? AND a.valid=1 ORDER BY a.id", (proposal["plan_id"],))]
                matches = [a for a in existing if a["route_id"] == route["id"] and a["route_version"] == route["version"]]
                if matches:
                    saved.append(matches[0])
                    continue
                approval = {"id": str(uuid4()), "post_id": current["post_id"], "content_hash": current["content_hash"],
                    "destination_id": route["destination_id"], "route_id": route["id"], "route_version": route["version"],
                    "account_id": route["account_id"], "account_epoch": route["account_epoch"], "sender": route["sender"]}
                Outbox(self.db).save_approval(approval, connection=con)
                con.execute("INSERT INTO routing_approval_context VALUES (?,?)", (approval["id"], proposal["plan_id"]))
                saved.append(approval)
            return saved[0]

    def revoke(self, approval_id):
        with self.db.transaction() as con:
            con.execute("UPDATE approvals SET valid=0 WHERE id=?", (approval_id,))
