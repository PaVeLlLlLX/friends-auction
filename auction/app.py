"""Auction HTTP boundary: signed identity, private approval, and serialized bids."""

import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import warnings
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Literal, ParamSpec, TypeVar, cast, overload

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    render_template_string,
    request,
    send_from_directory,
)
from flask.typing import ResponseReturnValue
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from werkzeug.exceptions import HTTPException

from auction import __version__
from auction.contracts import JSONObject, JSONValue
from auction.i18n import translate
from auction.models import (
    Candidate,
    LotRecord,
    PublicLot,
    TelegramIdentity,
    UserRecord,
    bid_history_from_row,
    candidate_from_row,
    lot_from_row,
    personal_bid_from_row,
    user_from_row,
)

P = ParamSpec("P")
R = TypeVar("R")

ROOT = Path(__file__).resolve().parent.parent
END = datetime(2026, 12, 12, 16, 59, tzinfo=timezone.utc).timestamp()
DELETE_AFTER = datetime(2027, 1, 31, 17, 0, tzinfo=timezone.utc).timestamp()
Image.MAX_IMAGE_PIXELS = 20_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)
register_heif_opener()

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','pending','approved','rejected')), applicant_given_name TEXT NOT NULL DEFAULT '', applicant_family_name TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS lots (id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, condition TEXT NOT NULL, start_price INTEGER NOT NULL, step INTEGER NOT NULL, created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled')), fulfillment TEXT NOT NULL DEFAULT '', refused TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS photos (id TEXT PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE, filename TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bids (id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id), user_id INTEGER NOT NULL REFERENCES users(id), amount INTEGER NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS bids_lot ON bids(lot_id, amount DESC, id DESC);
CREATE TABLE IF NOT EXISTS subscriptions (lot_id INTEGER NOT NULL REFERENCES lots(id), user_id INTEGER NOT NULL REFERENCES users(id), participation INTEGER NOT NULL DEFAULT 0, watching INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(lot_id,user_id));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id), user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, created REAL NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (name TEXT PRIMARY KEY, completed REAL NOT NULL);
"""


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open an autocommit connection with foreign keys and a bounded busy wait.

    The caller owns closing it; multi-statement mutations must explicitly begin
    and commit or roll back a transaction. SQLite failures propagate.
    """
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    return db


def verify_telegram(raw: str, token: str, now: float | None = None) -> TelegramIdentity | None:
    """Verify signed initData and its 24-hour age before returning a limited profile.

    Malformed, duplicate, expired, or incorrectly signed input returns None.
    Future timestamps allow up to 30 seconds of clock skew. This authenticates
    identity only; it grants no auction role.
    """
    if not token or not raw or len(raw) > 8192:
        return None
    try:
        pairs = urllib.parse.parse_qsl(raw, strict_parsing=True, keep_blank_values=True)
        values = dict(pairs)
        if len(values) != len(pairs):
            return None
        supplied = values.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", supplied):
            return None
        check = "\n".join(k + "=" + v for k, v in sorted(values.items()))
        key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            return None
        auth_date = int(values["auth_date"])
        now = time.time() if now is None else now
        if auth_date > now + 30 or now - auth_date > 86400:
            return None
        user: JSONValue = json.loads(values["user"])
        if not isinstance(user, dict):
            return None
        user_id = user.get("id")
        if type(user_id) is not int or not 0 < user_id <= 2**63 - 1:
            return None
        return {
            "id": user_id,
            "first_name": str(user.get("first_name", ""))[:100],
            "username": str(user.get("username") or "")[:64],
        }
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def create_app(config: Mapping[str, object] | None = None) -> Flask:
    """Build the HTTP application and initialize its configured local storage.

    Config overrides environment defaults, including for isolated tests. Startup
    creates directories and applies the additive names migration without deleting
    existing accounts. Missing production credentials raise RuntimeError; storage
    errors propagate. No delivery worker or external Telegram call starts here.
    """
    app = Flask(__name__, static_folder=None)
    app.config.update(
        DB=os.environ.get("AUCTION_DB", str(ROOT / "data" / "auction.sqlite3")),
        UPLOADS=os.environ.get("AUCTION_UPLOADS", str(ROOT / "data" / "photos")),
        BOT_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        ADMIN_ID=os.environ.get("TELEGRAM_ADMIN_ID", ""),
        DEMO=os.environ.get("AUCTION_DEMO") == "1",
        TRIAL=os.environ.get("AUCTION_TRIAL") == "1",
        CLOUDFLARE_PROXY=os.environ.get("AUCTION_CLOUDFLARE_PROXY") == "1",
        END=END,
        DELETE_AFTER=DELETE_AFTER,
        TESTING=False,
        MAX_CONTENT_LENGTH=36 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=500 * 1024,
        MAX_FORM_PARTS=12,
    )
    if config:
        app.config.update(config)
    if (
        not app.config["TESTING"]
        and not app.config["DEMO"]
        and (not app.config["BOT_TOKEN"] or not app.config["ADMIN_ID"])
    ):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID are required outside local demo mode."
        )
    Path(app.config["DB"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOADS"]).mkdir(parents=True, exist_ok=True)
    with closing(connect(app.config["DB"])) as db:
        db.executescript(SCHEMA)
        # Preserve existing accounts and applications when adding the private name fields.
        db.execute("BEGIN IMMEDIATE")
        try:
            columns = {column["name"] for column in db.execute("PRAGMA table_info(users)")}
            for column in ("applicant_given_name", "applicant_family_name"):
                if column not in columns:
                    db.execute(
                        "ALTER TABLE users ADD COLUMN " + column + " TEXT NOT NULL DEFAULT ''"
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise

    def database() -> sqlite3.Connection:
        """Reuse one request-scoped connection, owned by the app-context teardown hook."""
        if "db" not in g:
            g.db = connect(app.config["DB"])
        return cast(sqlite3.Connection, g.db)

    @app.teardown_appcontext
    def close_db(error: BaseException | None) -> None:
        """Close the request connection on success or failure; ignore the teardown error."""
        if "db" in g:
            g.db.close()

    @app.after_request
    def headers(resp: Response) -> Response:
        """Apply browser restrictions and prevent caching personalized API responses."""
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; object-src 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
        )
        resp.headers["Cache-Control"] = (
            "no-store" if request.path.startswith("/api/") else "no-cache"
        )
        return resp

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(410)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def errors(error: HTTPException) -> ResponseReturnValue:
        """Return API failures as JSON while preserving normal HTML error responses."""
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), cast(int, error.code)
        return error

    # Per-user and per-client limits supplement edge controls; they are not authorization.
    # Keep only bounded, current-minute entries, without clearing all limits on overflow.
    buckets: OrderedDict[tuple[int, str, str], int] = OrderedDict()
    bucket_lock = threading.Lock()

    def allow_rate(scope: str, subject: str, limit: int) -> bool:
        """Count one attempt in a locked, bounded per-process minute bucket.

        Return False once the limit is exceeded; this is abuse control rather than
        authentication, and does not coordinate separate web processes.
        """
        window = int(time.monotonic() // 60)
        key = (window, scope, subject)
        with bucket_lock:
            while buckets and next(iter(buckets))[0] < window:
                buckets.popitem(last=False)
            count = buckets.get(key, 0) + 1
            buckets[key] = count
            while len(buckets) > 10000:
                buckets.popitem(last=False)
            return count <= limit

    def cloudflare_client_ip() -> str | None:
        """Trust CF-Connecting-IP only for an explicitly enabled loopback tunnel.

        Return None for an invalid address or an untrusted connection so ordinary
        request addressing remains the fallback.
        """
        # Both supported tunnel modes bind the listener to loopback.
        # Never trust this header merely because a public client supplied it.
        if not (app.config["TRIAL"] or app.config["CLOUDFLARE_PROXY"]):
            return None
        try:
            if not ipaddress.ip_address(request.remote_addr or "").is_loopback:
                return None
            return str(ipaddress.ip_address(request.headers.get("CF-Connecting-IP", "")))
        except ValueError:
            return None

    @app.before_request
    def throttle() -> None:
        """Enforce demo isolation, retention expiry, and request/application rate limits.

        Cache verified identity in the request context, but leave account approval
        to the action handlers. Rejected requests raise localized HTTP errors.
        """
        if app.config["DEMO"] and (
            request.host.split(":")[0] not in ("localhost", "127.0.0.1")
            or request.headers.get("X-Forwarded-For")
            or request.headers.get("Forwarded")
        ):
            abort(403, translate("error_demo_remote_access"))
        if time.time() >= app.config["DELETE_AFTER"] and (
            request.path.startswith("/api/") or request.path.startswith("/photos/")
        ):
            abort(410, translate("error_retention_expired"))
        if request.path.startswith("/api/"):
            user = identity()
            g.verified_identity = user
            write = request.method not in ("GET", "HEAD", "OPTIONS")
            if user:
                subject = str(user["id"])
                scope, limit = ("user-write", 90) if write else ("user-read", 300)
            else:
                subject = cloudflare_client_ip() or request.remote_addr or "unknown"
                scope, limit = ("guest-write", 60) if write else ("guest-read", 300)
            if not allow_rate(scope, subject, limit):
                abort(429, translate("error_rate_limit"))
            # A single connection should not create an unbounded stream of applications.
            # It never shares a bucket with administrator actions or ordinary browsing.
            if request.path == "/api/application" and write and user:
                ip = cloudflare_client_ip()
                if not allow_rate("application-user", subject, 5) or (
                    ip and not allow_rate("application-ip", ip, 20)
                ):
                    abort(429, translate("error_application_rate_limit"))

    def identity() -> TelegramIdentity | None:
        """Resolve a signed identity, or an explicit local-demo role.

        Do not infer organizer access or approval from a Telegram username.
        """
        if app.config["DEMO"]:
            # Explicit demo mode only, must never be exposed on a public host.
            role = request.headers.get("X-Demo-User", "guest")
            sample = {
                "admin": (90001, "organizer", translate("demo_admin_name")),
                "friend": (90002, "friend", translate("demo_first_participant_name")),
                "other": (90003, "another_friend", translate("demo_second_participant_name")),
            }
            if role in sample:
                uid, username, name = sample[role]
                return {"id": uid, "username": username, "first_name": name}
        return verify_telegram(
            request.headers.get("X-Telegram-Init-Data", ""), app.config["BOT_TOKEN"]
        )

    @overload
    def current_user(required: Literal[True]) -> UserRecord:
        """Require signed access, aborting 401 instead of returning an absent account."""
        ...

    @overload
    def current_user(required: Literal[False] = False) -> UserRecord | None:
        """Permit anonymous browsing while returning a private row for signed access."""
        ...

    def current_user(required: bool = False) -> UserRecord | None:
        """Load or create the authenticated account and refresh its Telegram profile.

        Application names and approval status remain untouched. Optional anonymous
        access returns None; required anonymous access aborts with HTTP 401.
        """
        user = cast(TelegramIdentity | None, g.get("verified_identity"))
        if user is None:
            if required:
                abort(401, translate("error_telegram_login_required"))
            return None
        db = database()
        row = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO users(id, username, first_name) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
                (user["id"], user["username"], user["first_name"]),
            )
            row = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        elif row["username"] != user["username"] or row["first_name"] != user["first_name"]:
            db.execute(
                "UPDATE users SET username=?, first_name=? WHERE id=?",
                (user["username"], user["first_name"], user["id"]),
            )
            row = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        return user_from_row(row)

    def admin_required(fn: Callable[P, R]) -> Callable[P, R]:
        """Preserve the view signature while requiring the configured organizer ID."""

        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            """Reject anonymous or non-organizer callers before invoking the wrapped view."""
            user = current_user(True)
            expected = "90001" if app.config["DEMO"] else str(app.config["ADMIN_ID"])
            if not expected or str(user["id"]) != expected:
                abort(403, translate("error_access_denied"))
            return fn(*args, **kwargs)

        return wrapper

    def is_admin(user: UserRecord | None) -> bool:
        """Compare the numeric account identity with the configured organizer identity."""
        return bool(
            user
            and str(user["id"]) == ("90001" if app.config["DEMO"] else str(app.config["ADMIN_ID"]))
            and (app.config["DEMO"] or app.config["ADMIN_ID"])
        )

    def active_lot(lot_id: int) -> LotRecord:
        """Load an existing lot or abort 404, including cancelled and ended lots.

        The name does not imply bidding eligibility; mutation handlers must check
        status and deadline themselves.
        """
        if lot_id > 2**63 - 1:
            abort(404, translate("error_lot_not_found"))
        lot = database().execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
        if not lot:
            abort(404, translate("error_lot_not_found"))
        return lot_from_row(lot)

    def json_body() -> JSONObject:
        """Require a JSON object and expose values for action-specific validation.

        Nested fields remain untrusted; malformed or non-object input aborts 400.
        """
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, translate("error_invalid_request"))
        return cast(JSONObject, data)

    def require_candidate(data: JSONObject, candidate_id: int) -> None:
        """Reject a missing or stale selected candidate before an organizer action.

        The caller must hold its mutation transaction when candidate stability is
        required; this helper performs no locking or delivery itself.
        """
        expected = data.get("expected_candidate_id")
        if type(expected) is not int or not 0 < expected <= 2**63 - 1:
            abort(400, translate("error_candidate_required"))
        if expected != candidate_id:
            abort(409, translate("error_candidate_changed"))

    def lot_view(lot: LotRecord, user: UserRecord | None = None) -> PublicLot:
        """Build a public lot projection without bidder identities or application names.

        Personalized booleans describe only this caller. After a refusal, price and
        leading follow the highest remaining candidate while count preserves all bids.
        """
        db = database()
        lead = db.execute(
            "SELECT amount, user_id FROM bids WHERE lot_id=? ORDER BY amount DESC,id DESC LIMIT 1",
            (lot["id"],),
        ).fetchone()
        photos = [
            ("/photos/" + x["id"])
            for x in db.execute("SELECT id FROM photos WHERE lot_id=? ORDER BY rowid", (lot["id"],))
        ]
        sub = (
            db.execute(
                "SELECT participation, watching FROM subscriptions WHERE lot_id=? AND user_id=?",
                (lot["id"], user["id"]),
            ).fetchone()
            if user
            else None
        )
        has_bid = bool(
            user
            and db.execute(
                "SELECT 1 FROM bids WHERE lot_id=? AND user_id=? LIMIT 1", (lot["id"], user["id"])
            ).fetchone()
        )
        refused = cast(list[int], json.loads(lot["refused"]))
        candidate_id = cast(int, lead["user_id"]) if lead else None
        price = cast(int, lead["amount"]) if lead else None
        if refused:
            candidates = candidates_for(db, lot["id"], refused)
            candidate_id = candidates[0]["id"] if candidates else None
            price = candidates[0]["amount"] if candidates else None
        return PublicLot(
            id=lot["id"],
            title=lot["title"],
            description=lot["description"],
            condition=lot["condition"],
            start_price=lot["start_price"],
            step=lot["step"],
            price=price,
            count=cast(
                int,
                db.execute("SELECT count(*) FROM bids WHERE lot_id=?", (lot["id"],)).fetchone()[0],
            ),
            photos=photos,
            status="cancelled"
            if lot["status"] == "cancelled"
            else ("ended" if time.time() >= app.config["END"] else "active"),
            leading=bool(user and candidate_id == user["id"] and lot["status"] == "active"),
            has_bid=has_bid,
            participation=bool(sub and sub["participation"]),
            watching=bool(sub and sub["watching"]),
        )

    @app.get("/")
    def index() -> str:
        """Render the shared localized shell; catalog data is fetched through the API."""
        template = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        return render_template_string(template, translate=translate)

    @app.get("/assets/<path:filename>")
    def assets(filename: str) -> Response:
        """Serve a static asset using directory confinement; missing files abort 404."""
        return send_from_directory(ROOT / "static", filename)

    @app.get("/photos/<photo_id>")
    def photo(photo_id: str) -> Response:
        """Serve only a registered, reencoded JPEG; unknown photo IDs abort 404.

        The request hook blocks access after retention expiry.
        """
        row = (
            database()
            .execute(
                "SELECT p.filename FROM photos p JOIN lots l ON l.id=p.lot_id WHERE p.id=?",
                (photo_id,),
            )
            .fetchone()
        )
        if not row:
            abort(404)
        return send_from_directory(app.config["UPLOADS"], row["filename"], mimetype="image/jpeg")

    @app.get("/api/me")
    def me() -> Response:
        """Expose the caller profile and capabilities without private application names."""
        user = current_user()
        return jsonify(
            user={
                "first_name": user["first_name"],
                "username": user["username"],
                "status": user["status"],
                "admin": is_admin(user),
            }
            if user
            else None,
            end=app.config["END"],
            demo=app.config["DEMO"],
            trial=app.config["TRIAL"],
        )

    @app.get("/api/version")
    def version() -> Response:
        """Return the packaged application version without deployment credentials."""
        return jsonify(version=__version__)

    @app.get("/api/lots")
    def lots() -> Response:
        """List public lot projections, optionally personalized for the signed caller."""
        user = current_user()
        rows = (
            database()
            .execute(
                "SELECT * FROM lots ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC"
            )
            .fetchall()
        )
        return jsonify(
            lots=[lot_view(lot_from_row(row), user) for row in rows], end=app.config["END"]
        )

    @app.get("/api/lots/<int:lot_id>")
    def get_lot(lot_id: int) -> Response:
        """Return one public projection or abort 404 without disclosing bidder profiles."""
        return jsonify(lot=lot_view(active_lot(lot_id), current_user()))

    @app.post("/api/application")
    def application() -> Response:
        """Persist private names only for a new authenticated account.

        Validate both names and condition the update on new status so repeated
        submissions preserve an existing application or organizer decision.
        """
        user = current_user(True)
        if user["status"] == "new":
            data = json_body()
            given = data.get("given_name")
            family = data.get("family_name")
            if not isinstance(given, str) or not isinstance(family, str):
                abort(400, translate("error_names_required"))
            given, family = given.strip(), family.strip()
            if any(
                len(name) > 100
                or not any(char.isalpha() for char in name)
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                for name in (given, family)
            ):
                abort(400, translate("error_names_invalid"))
            database().execute(
                "UPDATE users SET status='pending', applicant_given_name=?, applicant_family_name=? WHERE id=? AND status='new'",
                (given, family, user["id"]),
            )
        return jsonify(
            status=database()
            .execute("SELECT status FROM users WHERE id=?", (user["id"],))
            .fetchone()["status"]
        )

    @app.post("/api/lots/<int:lot_id>/bid")
    def bid(lot_id: int) -> Response:
        """Record an approved caller bid and notification events in one write transaction.

        Recheck approval, lot status, exact observed price, step, and strict deadline
        under the SQLite write lock. Failures roll back the bid and subscriptions;
        HTTP 409 reports a stale or closed auction rather than silently repricing.
        """
        user = current_user(True)
        if user["status"] != "approved":
            abort(403, translate("error_approval_required"))
        payload = json_body()
        amount = payload.get("amount")
        expected = payload.get("expected_price")
        if (
            type(amount) is not int
            or amount < 0
            or amount > 100_000_000
            or (expected is not None and type(expected) is not int)
        ):
            abort(400, translate("error_bid_amount_invalid"))
        db = database()
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("SELECT status FROM users WHERE id=?", (user["id"],)).fetchone()
            if not current or current["status"] != "approved":
                abort(403, translate("error_approval_required"))
            lot = active_lot(lot_id)
            if lot["status"] != "active" or time.time() >= app.config["END"]:
                abort(409, translate("error_bidding_closed"))
            lead = db.execute(
                "SELECT amount,user_id FROM bids WHERE lot_id=? ORDER BY amount DESC,id DESC LIMIT 1",
                (lot_id,),
            ).fetchone()
            price = cast(int, lead["amount"]) if lead else None
            if expected != price:
                abort(409, translate("error_bid_price_changed"))
            if lead and lead["user_id"] == user["id"]:
                abort(409, translate("error_already_leading"))
            base = price if price is not None else lot["start_price"]
            minimum = base + lot["step"] if lead else base
            if amount < minimum or (amount - base) % lot["step"] != 0:
                abort(400, translate("error_bid_step_invalid"))
            now = time.time()
            if now >= app.config["END"]:
                abort(409, translate("error_bidding_closed"))
            db.execute(
                "INSERT INTO bids(lot_id,user_id,amount,created) VALUES(?,?,?,?)",
                (lot_id, user["id"], amount, now),
            )
            db.execute(
                "INSERT INTO subscriptions(lot_id,user_id,participation,watching) VALUES(?,?,1,0) ON CONFLICT(lot_id,user_id) DO UPDATE SET participation=1",
                (lot_id, user["id"]),
            )
            watchers = db.execute(
                "SELECT user_id, participation, watching FROM subscriptions WHERE lot_id=? AND user_id!=?",
                (lot_id, user["id"]),
            ).fetchall()
            for watcher in watchers:
                if lead and watcher["user_id"] == lead["user_id"] and watcher["participation"]:
                    db.execute(
                        "INSERT INTO events(lot_id,user_id,kind,created) VALUES(?,?,'outbid',?)",
                        (lot_id, watcher["user_id"], now),
                    )
                if watcher["watching"]:
                    db.execute(
                        "INSERT INTO events(lot_id,user_id,kind,created) VALUES(?,?,'watch',?)",
                        (lot_id, watcher["user_id"], now),
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(lot=lot_view(active_lot(lot_id), user))

    @app.post("/api/lots/<int:lot_id>/subscription")
    def subscription(lot_id: int) -> Response:
        """Change only the authenticated caller notification flags.

        Participation can be enabled only after that caller has bid; watching does
        not require approval. Whitelisted flag names alone enter the SQL statement.
        """
        user = current_user(True)
        active_lot(lot_id)
        data = json_body()
        kind = data.get("kind")
        enabled = data.get("enabled")
        if (
            not isinstance(kind, str)
            or kind not in ("watching", "participation")
            or type(enabled) is not bool
        ):
            abort(400, translate("error_subscription_invalid"))
        db = database()
        if (
            kind == "participation"
            and enabled
            and not db.execute(
                "SELECT 1 FROM bids WHERE lot_id=? AND user_id=?", (lot_id, user["id"])
            ).fetchone()
        ):
            abort(400, translate("error_participation_requires_bid"))
        db.execute(
            "INSERT OR IGNORE INTO subscriptions(lot_id,user_id) VALUES(?,?)", (lot_id, user["id"])
        )
        db.execute(
            "UPDATE subscriptions SET " + kind + "=? WHERE lot_id=? AND user_id=?",
            (int(enabled), lot_id, user["id"]),
        )
        return jsonify(ok=True)

    @app.get("/api/my-bids")
    def my_bids() -> Response:
        """Return the caller's complete bid history without other participants' profiles."""
        user = current_user(True)
        db = database()
        rows = db.execute(
            "SELECT b.id,b.amount,b.created,b.lot_id,l.title FROM bids b JOIN lots l ON l.id=b.lot_id WHERE b.user_id=? ORDER BY b.id DESC",
            (user["id"],),
        ).fetchall()
        return jsonify(bids=[personal_bid_from_row(row) for row in rows])

    @app.get("/api/admin/applications")
    @admin_required
    def admin_applications() -> Response:
        """Expose application names and decisions only behind organizer authorization."""
        users = (
            database()
            .execute(
                "SELECT id,username,first_name,status,applicant_given_name,applicant_family_name FROM users WHERE status!='new' ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC"
            )
            .fetchall()
        )
        return jsonify(users=[user_from_row(row) for row in users])

    @app.post("/api/admin/users/<int:user_id>")
    @admin_required
    def admin_user(user_id: int) -> Response:
        """Approve or reject an existing account without rewriting any prior bids.

        Unknown users abort 404; decisions other than approved/rejected abort 400.
        """
        if user_id > 2**63 - 1:
            abort(404, translate("error_application_not_found"))
        status = json_body().get("status")
        if status not in ("approved", "rejected"):
            abort(400, translate("error_application_decision_invalid"))
        result = database().execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
        if result.rowcount == 0:
            abort(404, translate("error_application_not_found"))
        return jsonify(ok=True)

    @app.post("/api/admin/lots")
    @admin_required
    def create_lot() -> tuple[Response, int]:
        """Validate and reencode organizer uploads before committing a new lot.

        Strip source metadata, enforce image/request bounds, and recheck closing time
        inside the write transaction. On failure roll back rows and remove attempted
        photo files, including partial writes; storage failures propagate.
        """
        if time.time() >= app.config["END"]:
            abort(409, translate("error_auction_closed"))
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        condition = request.form.get("condition", "").strip()
        try:
            start = int(request.form.get("start_price", ""))
            step = int(request.form.get("step", ""))
        except ValueError:
            abort(400, translate("error_lot_price_invalid"))
        if (
            not title
            or len(title) > 120
            or len(description) > 3000
            or len(condition) > 120
            or start < 0
            or start > 100_000_000
            or step < 1
            or step > 100_000_000
        ):
            abort(400, translate("error_lot_fields_invalid"))
        images = request.files.getlist("photos")
        if len(images) > 6:
            abort(400, translate("error_photo_limit"))
        prepared: list[bytes] = []
        for uploaded in images:
            if not uploaded.filename:
                continue
            try:
                img: Image.Image = Image.open(uploaded.stream)
                if img.format not in ("JPEG", "PNG", "WEBP", "HEIF"):
                    abort(400, translate("error_photo_format"))
                img = ImageOps.exif_transpose(img)
                img.thumbnail((1800, 1800))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                # JPEG comments can survive reencoding unless inherited metadata is cleared.
                img.info.clear()
                buffer = io.BytesIO()
                img.save(buffer, "JPEG", quality=85, optimize=True)
                prepared.append(buffer.getvalue())
            except (
                UnidentifiedImageError,
                OSError,
                ValueError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ):
                abort(400, translate("error_photo_unreadable"))
        db = database()
        paths: list[Path] = []
        try:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if now >= app.config["END"]:
                abort(409, translate("error_auction_closed"))
            lot_id = db.execute(
                "INSERT INTO lots(title,description,condition,start_price,step,created) VALUES(?,?,?,?,?,?)",
                (title, description, condition, start, step, now),
            ).lastrowid
            assert lot_id is not None
            for content in prepared:
                photo_id = secrets.token_hex(16)
                filename = photo_id + ".jpg"
                path = Path(app.config["UPLOADS"]) / filename
                paths.append(path)
                path.write_bytes(content)
                db.execute(
                    "INSERT INTO photos(id,lot_id,filename) VALUES(?,?,?)",
                    (photo_id, lot_id, filename),
                )
            db.commit()
        except Exception:
            db.rollback()
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return jsonify(lot=lot_view(active_lot(lot_id))), 201

    @app.post("/api/admin/lots/<int:lot_id>")
    @admin_required
    def admin_lot(lot_id: int) -> Response:
        """Serialize organizer cancellation, fulfillment, and candidate refusal changes.

        Refusal requires closed active bidding and the exact displayed candidate;
        it preserves bids and clears the previous candidate fulfillment state.
        Every failure rolls back the transaction.
        """
        data = json_body()
        action = data.get("action")
        db = database()
        db.execute("BEGIN IMMEDIATE")
        try:
            lot = active_lot(lot_id)
            if action == "cancel":
                db.execute("UPDATE lots SET status='cancelled' WHERE id=?", (lot_id,))
            elif action in ("agreed", "delivered", ""):
                db.execute("UPDATE lots SET fulfillment=? WHERE id=?", (action, lot_id))
            elif action == "refuse":
                if time.time() < app.config["END"] or lot["status"] != "active":
                    abort(409, translate("error_results_not_ready"))
                refused = cast(list[int], json.loads(lot["refused"]))
                candidates = candidates_for(db, lot_id, refused)
                if not candidates:
                    abort(409, translate("error_no_candidates"))
                require_candidate(data, candidates[0]["id"])
                refused.append(candidates[0]["id"])
                db.execute(
                    "UPDATE lots SET refused=?, fulfillment='' WHERE id=?",
                    (json.dumps(refused), lot_id),
                )
            else:
                abort(400, translate("error_lot_action_invalid"))
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(ok=True)

    @app.post("/api/admin/lots/<int:lot_id>/edit")
    @admin_required
    def edit_lot(lot_id: int) -> Response:
        """Update validated lot fields only before closing and before its first bid.

        The eligibility check and update share a write lock so a concurrent bid
        cannot acquire changed terms. Validation/conflict failures leave rows intact.
        """
        data = json_body()
        title = data.get("title", "")
        description = data.get("description", "")
        condition = data.get("condition", "")
        start, step = data.get("start_price"), data.get("step")
        if (
            not isinstance(title, str)
            or not isinstance(description, str)
            or not isinstance(condition, str)
        ):
            abort(400, translate("error_lot_description_invalid"))
        title, description, condition = title.strip(), description.strip(), condition.strip()
        if (
            not title
            or len(title) > 120
            or len(description) > 3000
            or len(condition) > 120
            or type(start) is not int
            or type(step) is not int
            or not 0 <= start <= 100_000_000
            or not 1 <= step <= 100_000_000
        ):
            abort(400, translate("error_lot_fields_invalid"))
        db = database()
        db.execute("BEGIN IMMEDIATE")
        try:
            lot = active_lot(lot_id)
            if (
                lot["status"] != "active"
                or time.time() >= app.config["END"]
                or db.execute("SELECT 1 FROM bids WHERE lot_id=? LIMIT 1", (lot_id,)).fetchone()
            ):
                abort(409, translate("error_lot_edit_locked"))
            db.execute(
                "UPDATE lots SET title=?,description=?,condition=?,start_price=?,step=? WHERE id=?",
                (title, description, condition, start, step, lot_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(ok=True)

    @app.get("/api/admin/lots/<int:lot_id>")
    @admin_required
    def admin_lot_detail(lot_id: int) -> Response:
        """Return private candidate profiles and full bid history to the organizer only."""
        lot = active_lot(lot_id)
        db = database()
        return jsonify(
            lot=lot_view(lot),
            candidates=candidates_for(db, lot_id, cast(list[int], json.loads(lot["refused"]))),
            has_refusals=bool(json.loads(lot["refused"])),
            fulfillment=lot["fulfillment"],
            bids=[
                bid_history_from_row(r)
                for r in db.execute(
                    "SELECT b.amount,b.created,u.id,u.username,u.first_name,u.applicant_given_name,u.applicant_family_name FROM bids b JOIN users u ON u.id=b.user_id WHERE b.lot_id=? ORDER BY b.amount DESC",
                    (lot_id,),
                )
            ],
        )

    @app.post("/api/admin/lots/<int:lot_id>/notify-candidate")
    @admin_required
    def notify_candidate(lot_id: int) -> Response:
        """Send an organizer-requested offer to the explicitly selected fallback.

        Only closed active lots with a recorded refusal qualify. Check the current
        candidate before an external Telegram send; delivery failure aborts 409.
        The send is not transactional with a simultaneous later refusal.
        """
        lot = active_lot(lot_id)
        if time.time() < app.config["END"] or lot["status"] != "active":
            abort(409, translate("error_results_not_ready"))
        candidates = candidates_for(database(), lot_id, cast(list[int], json.loads(lot["refused"])))
        if not candidates or not json.loads(lot["refused"]):
            abort(409, translate("error_fallback_not_selected"))
        require_candidate(json_body(), candidates[0]["id"])
        from auction.worker import send_message

        if not send_message(
            app.config["BOT_TOKEN"],
            candidates[0]["id"],
            translate("notification_candidate_offer_prefix")
            + lot["title"]
            + translate("notification_candidate_offer_suffix"),
        ):
            abort(409, translate("error_notification_delivery"))
        return jsonify(ok=True)

    return app


def candidates_for(db: sqlite3.Connection, lot_id: int, refused: Sequence[int]) -> list[Candidate]:
    """Rank non-refused participants by each participant's highest recorded bid.

    Return private organizer-facing profiles without changing bids or approval;
    the caller owns the connection and any transaction needed for stability.
    """
    rows = db.execute(
        "SELECT u.id,u.username,u.first_name,u.applicant_given_name,u.applicant_family_name,MAX(b.amount) AS amount FROM bids b JOIN users u ON u.id=b.user_id WHERE b.lot_id=? GROUP BY u.id ORDER BY amount DESC",
        (lot_id,),
    ).fetchall()
    return [candidate_from_row(row) for row in rows if row["id"] not in refused]
