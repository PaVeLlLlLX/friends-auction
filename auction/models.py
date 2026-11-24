"""Records crossing authenticated, persisted, and public auction boundaries.

SQLite adapters describe the schema owned by this application; they do not
validate or repair an externally modified database. PublicLot deliberately has
no participant identifiers or application names.
"""

import sqlite3
from typing import Literal, TypedDict, cast


class TelegramIdentity(TypedDict):
    """Signed Telegram profile, before any application or approval lookup."""

    id: int
    username: str
    first_name: str


class UserRecord(TypedDict):
    """Private account row; application names may be shown only to the organizer."""

    id: int
    username: str | None
    first_name: str
    status: Literal["new", "pending", "approved", "rejected"]
    applicant_given_name: str
    applicant_family_name: str


class LotRecord(TypedDict):
    """Persisted lot fields, including organizer-only fulfillment and refusals."""

    id: int
    title: str
    description: str
    condition: str
    start_price: int
    step: int
    created: float
    status: Literal["active", "cancelled"]
    fulfillment: str
    refused: str
    deleted: int
    revision: int


class Candidate(TypedDict):
    """Organizer-only participant profile and that participant's highest bid."""

    id: int
    username: str | None
    first_name: str
    applicant_given_name: str
    applicant_family_name: str
    amount: int


class BidHistoryEntry(Candidate):
    """One organizer-visible bid, retaining its original timestamp and owner."""

    created: float


class PersonalBid(TypedDict):
    """Caller-owned bid history without other participants' identifying fields."""

    id: int
    amount: int
    created: float
    lot_id: int
    title: str
    deleted: bool
    status: Literal["active", "ended", "cancelled"]
    own_leading: bool


class CandidateNotification(TypedDict):
    """Private delivery reservation; unknown requires explicit organizer resolution."""

    candidate_id: int
    state: Literal["sending", "sent", "unknown", "not_sent"]


class PublicLot(TypedDict):
    """Catalog projection; personalized booleans disclose only the caller's state."""

    id: int
    title: str
    description: str
    condition: str
    start_price: int
    step: int
    price: int | None
    count: int
    photos: list[str]
    status: Literal["active", "ended", "cancelled"]
    leading: bool
    has_bid: bool
    participation: bool
    watching: bool
    revision: int


class AdminLot(PublicLot):
    """Organizer lot projection, retaining an explicit archive marker."""

    deleted: bool


def user_from_row(row: sqlite3.Row) -> UserRecord:
    """Project SELECT * FROM users using the application-owned users schema."""
    return cast(UserRecord, dict(row))


def lot_from_row(row: sqlite3.Row) -> LotRecord:
    """Retain persisted lot fields for internal checks and later public projection."""
    return cast(LotRecord, dict(row))


def candidate_from_row(row: sqlite3.Row) -> Candidate:
    """Type the grouped candidate query, retaining private organizer-only names."""
    return cast(Candidate, dict(row))


def personal_bid_from_row(
    row: sqlite3.Row, *, status: Literal["active", "ended", "cancelled"], own_leading: bool
) -> PersonalBid:
    """Project caller-owned history, excluding internal refusal IDs from the query."""
    return PersonalBid(
        id=row["id"],
        amount=row["amount"],
        created=row["created"],
        lot_id=row["lot_id"],
        title=row["title"],
        deleted=bool(row["deleted"]),
        status=status,
        own_leading=own_leading,
    )


def bid_history_from_row(row: sqlite3.Row) -> BidHistoryEntry:
    """Retain private owner fields in the organizer-authorized bid-history query."""
    return cast(BidHistoryEntry, dict(row))
