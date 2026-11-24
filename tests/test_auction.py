"""Behavioral and privacy checks using isolated databases and synthetic Telegram identities."""

import hashlib
import hmac
import io
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from threading import Event
from typing import NoReturn
from uuid import uuid4

import pytest
from flask import Flask, Request
from flask.testing import FlaskClient
from PIL import Image
from werkzeug.test import TestResponse

from auction.app import connect, create_app, verify_telegram
from auction.contracts import JSONObject, JSONValue
from auction.telegram import TelegramError, call
from auction.worker import tick

TOKEN = "123456:fake-test-token"


def json_object(value: JSONValue) -> JSONObject:
    """Require an object at a tested JSON boundary without claiming field validation."""
    assert isinstance(value, dict)
    return value


def json_response(response: TestResponse) -> JSONObject:
    """Decode an HTTP response and require a JSON object, including error responses."""
    value: JSONValue = response.get_json()
    return json_object(value)


def json_objects(value: JSONValue) -> list[JSONObject]:
    """Require an array of objects before assertions inspect their individual fields."""
    assert isinstance(value, list)
    return [json_object(item) for item in value]


def json_integer(value: JSONValue) -> int:
    """Require an integer value, excluding JSON booleans, for fixture IDs and offsets."""
    assert type(value) is int
    return value


def read_samples(path: Path) -> dict[str, str]:
    """Read the localized fixture dictionary and assert every fixture value is text."""
    value: JSONValue = json.loads(path.read_text(encoding="utf-8"))
    entries = json_object(value)
    result: dict[str, str] = {}
    for key, text in entries.items():
        assert isinstance(text, str)
        result[key] = text
    return result


SAMPLES = read_samples(Path(__file__).parent / "fixtures/messages.json")


def sample(key: str) -> str:
    """Read a named localized fixture value without changing runtime messages."""
    return SAMPLES[key]


def signed(
    user_id: int,
    name: str = sample("participant_name"),
    username: str = "friend",
    date: float | None = None,
) -> str:
    """Build Telegram initData signed with the test token and optional timestamp."""
    data = {
        "auth_date": str(int(time.time() if date is None else date)),
        "query_id": "test",
        "user": json.dumps(
            {"id": user_id, "first_name": name, "username": username},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def auth(user_id: int) -> dict[str, str]:
    """Return authorization headers for an isolated synthetic Telegram account."""
    return {"X-Telegram-Init-Data": signed(user_id, username=f"u{user_id}")}


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    """Create an auction with temporary storage and future test deadlines."""
    return create_app(
        {
            "TESTING": True,
            "DB": str(tmp_path / "db.sqlite3"),
            "UPLOADS": str(tmp_path / "photos"),
            "BOT_TOKEN": TOKEN,
            "ADMIN_ID": "1",
            "END": time.time() + 36000,
            "DELETE_AFTER": time.time() + 86400,
        }
    )


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    """Provide a Flask client without starting a network server."""
    return app.test_client()


def test_initialization_closes_bootstrap_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure application initialization closes its one-time database connection."""
    connections = []

    def observed_connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
        """Record bootstrap connections so their closure can be verified."""
        connection = connect(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr("auction.app.connect", observed_connect)
    create_app(
        {
            "TESTING": True,
            "DB": str(tmp_path / "bootstrap.sqlite3"),
            "UPLOADS": str(tmp_path / "photos"),
        }
    )
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def create_lot(client: FlaskClient, start: int = 0, step: int = 50) -> int:
    """Create a lot as the organizer and return its generated identifier."""
    res = client.post(
        "/api/admin/lots",
        headers=auth(1),
        data={
            "operation_id": str(uuid4()),
            "title": sample("lot_title"),
            "description": sample("lot_description"),
            "condition": sample("lot_condition"),
            "start_price": str(start),
            "step": str(step),
        },
    )
    assert res.status_code == 201, res.data
    return json_integer(json_object(json_response(res)["lot"])["id"])


def approve(client: FlaskClient, uid: int) -> None:
    """Submit private names and approve an account for all auction lots."""
    assert (
        client.post(
            "/api/application",
            headers=auth(uid),
            json={
                "given_name": sample("given_name"),
                "family_name": sample("family_name"),
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/admin/users/{uid}", headers=auth(1), json={"status": "approved"}
        ).status_code
        == 200
    )


def bid(
    client: FlaskClient, lot: int, uid: int, amount: int, expected: int | None, revision: int = 1
) -> TestResponse:
    """Submit an amount together with the price observed by the participant."""
    return client.post(
        f"/api/lots/{lot}/bid",
        headers=auth(uid),
        json={"amount": amount, "expected_price": expected, "expected_revision": revision},
    )


def test_telegram_signature_and_expiry() -> None:
    """Reject tampered, expired, malformed and incorrectly signed Telegram data."""
    raw = signed(42)
    identity = verify_telegram(raw, TOKEN)
    assert identity is not None and identity["id"] == 42
    assert verify_telegram(raw.replace("test", "evil"), TOKEN) is None
    assert verify_telegram(signed(42, date=time.time() - 86401), TOKEN) is None
    assert verify_telegram(raw, "wrong") is None
    assert verify_telegram(raw + "&hash=x", TOKEN) is None
    assert verify_telegram("auth_date=1&hash=%D0%B6", TOKEN) is None
    assert verify_telegram("auth_date=1&hash=" + "x" * 64, TOKEN) is None


def test_anonymous_flood_does_not_block_administrator(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep anonymous write throttling separate from organizer access."""
    monkeypatch.setattr("auction.app.time.monotonic", lambda: 6000)
    statuses = [client.post("/api/application", json={}).status_code for _ in range(61)]
    assert statuses[:60] == [401] * 60
    assert statuses[-1] == 429
    assert client.get("/api/admin/applications", headers=auth(1)).status_code == 200


def test_one_signed_user_cannot_exhaust_another_users_write_limit(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply signed write limits independently to each participant."""
    monkeypatch.setattr("auction.app.time.monotonic", lambda: 6000)
    lot = create_lot(client)
    payload = {"kind": "watching", "enabled": True}
    statuses = [
        client.post(f"/api/lots/{lot}/subscription", headers=auth(2), json=payload).status_code
        for _ in range(91)
    ]
    assert statuses[:90] == [200] * 90
    assert statuses[-1] == 429
    assert (
        client.post(f"/api/lots/{lot}/subscription", headers=auth(3), json=payload).status_code
        == 200
    )


@pytest.mark.parametrize("trial", [True, False])
def test_tunnel_application_flood_is_limited_per_cloudflare_client(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch, trial: bool
) -> None:
    """Limit applications behind explicit tunnels without blocking organizer reads."""
    monkeypatch.setattr("auction.app.time.monotonic", lambda: 6000)
    app.config["TRIAL"] = trial
    app.config["CLOUDFLARE_PROXY"] = not trial
    payload = {
        "given_name": sample("given_name"),
        "family_name": sample("family_name"),
    }

    def headers(uid: int, ip: str) -> dict[str, str]:
        """Combine a signed account with a simulated trusted-proxy client address."""
        return {**auth(uid), "CF-Connecting-IP": ip}

    assert all(
        client.post(
            "/api/application", headers=headers(uid, "203.0.113.8"), json=payload
        ).status_code
        == 200
        for uid in range(2, 22)
    )
    assert (
        client.post(
            "/api/application", headers=headers(22, "203.0.113.8"), json=payload
        ).status_code
        == 429
    )
    assert (
        client.post(
            "/api/application", headers=headers(23, "203.0.113.9"), json=payload
        ).status_code
        == 200
    )
    assert (
        client.get("/api/admin/applications", headers=headers(1, "203.0.113.8")).status_code == 200
    )


def test_oversized_lot_id_is_not_a_server_error(client: FlaskClient) -> None:
    """Return a missing-lot response for identifiers outside SQLite integer range."""
    assert client.get("/api/lots/" + "9" * 30).status_code == 404


@pytest.mark.parametrize("proxy, peer", [(False, "127.0.0.1"), (True, "203.0.113.20")])
def test_untrusted_forwarded_addresses_cannot_evade_guest_limits(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch, proxy: bool, peer: str
) -> None:
    """Ignore forged client addresses unless the explicit tunnel flag and loopback peer agree."""
    app.config.update(TRIAL=False, CLOUDFLARE_PROXY=proxy)
    monkeypatch.setattr("auction.app.time.monotonic", lambda: 6000)
    statuses = [
        client.post(
            "/api/application",
            json={},
            headers={"CF-Connecting-IP": f"198.51.100.{index}"},
            environ_base={"REMOTE_ADDR": peer},
        ).status_code
        for index in range(1, 62)
    ]
    assert statuses[:60] == [401] * 60
    assert statuses[-1] == 429


def test_zero_bid_step_approval_and_private_data(client: FlaskClient) -> None:
    """Enforce admission, bid increments and stale-price checks without identity leaks."""
    lot = create_lot(client)
    assert bid(client, lot, 2, 0, None).status_code == 403
    approve(client, 2)
    assert bid(client, lot, 2, 0, None).status_code == 200
    approve(client, 3)
    assert bid(client, lot, 3, 25, 0).status_code == 400
    assert bid(client, lot, 3, 100, 0).status_code == 200
    assert bid(client, lot, 2, 150, 0).status_code == 409
    public = client.get(f"/api/lots/{lot}").get_data(as_text=True)
    assert "user_id" not in public and "u2" not in public and "u3" not in public
    assert client.get("/api/admin/applications", headers=auth(2)).status_code == 403
    assert client.get(f"/api/admin/lots/{lot}", headers=auth(3)).status_code == 403
    mine = json_objects(json_response(client.get("/api/my-bids", headers=auth(2)))["bids"])
    assert [b["amount"] for b in mine] == [0]
    assert json_object(json_response(client.get(f"/api/lots/{lot}", headers=auth(3)))["lot"])[
        "leading"
    ]


def test_concurrent_bids_only_one_wins(app: Flask, client: FlaskClient) -> None:
    """Accept only one of two bids made against the same observed price."""
    lot = create_lot(client, start=100)
    approve(client, 2)
    approve(client, 3)

    def run(uid: int) -> int:
        """Submit a competing bid through an independent request context."""
        with app.test_client() as c:
            return bid(c, lot, uid, 100, None).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [2, 3]))
    assert sorted(results) == [200, 409]
    assert json_object(json_response(client.get(f"/api/lots/{lot}"))["lot"])["count"] == 1


def test_close_is_strict_and_no_late_bid(app: Flask, client: FlaskClient) -> None:
    """Close the lot at its deadline and reject subsequent bids."""
    lot = create_lot(client)
    approve(client, 2)
    app.config["END"] = time.time() - 1
    assert bid(client, lot, 2, 0, None).status_code == 409
    assert json_object(json_response(client.get(f"/api/lots/{lot}"))["lot"])["status"] == "ended"


def test_bid_cannot_cross_deadline_during_request(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a bid whose preparation starts before but completes after closing."""
    lot = create_lot(client)
    approve(client, 2)
    headers = auth(2)
    before = time.time()
    app.config["END"] = before + 1
    calls: list[None] = []

    def clock() -> float:
        """Advance past the deadline after request preparation has completed."""
        calls.append(None)
        # Expiry guard, authentication, and first bid check all precede close.
        return before if len(calls) <= 3 else app.config["END"]

    monkeypatch.setattr("auction.app.time.time", clock)
    response = client.post(
        f"/api/lots/{lot}/bid",
        headers=headers,
        json={"amount": 0, "expected_price": None, "expected_revision": 1},
    )
    assert response.status_code == 409
    db = connect(app.config["DB"])
    try:
        assert db.execute("SELECT count(*) FROM bids").fetchone()[0] == 0
    finally:
        db.close()


def test_lot_creation_rechecks_deadline_after_preparation(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a prepared lot from being committed after the auction closes."""
    headers = auth(1)
    before = time.time()
    app.config["END"] = before + 1
    calls: list[None] = []

    def clock() -> float:
        """Advance past the deadline after request preparation has completed."""
        calls.append(None)
        return before if len(calls) <= 3 else app.config["END"]

    monkeypatch.setattr("auction.app.time.time", clock)
    response = client.post(
        "/api/admin/lots",
        headers=headers,
        data={"operation_id": str(uuid4()), "title": "Test lot", "start_price": "0", "step": "1"},
    )
    assert response.status_code == 409
    db = connect(app.config["DB"])
    try:
        assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 0
    finally:
        db.close()


def test_photo_is_reencoded_and_rejects_fake_image(app: Flask, client: FlaskClient) -> None:
    """Strip source metadata from valid photos and reject undecodable uploads."""
    image = Image.new("RGB", (15, 15), "red")
    output = io.BytesIO()
    private_comment = b"private-original-comment"
    image.save(output, "JPEG", exif=b"Exif\x00\x00junk", comment=private_comment)
    assert private_comment in output.getvalue()
    output.seek(0)
    res = client.post(
        "/api/admin/lots",
        headers=auth(1),
        data={
            "operation_id": str(uuid4()),
            "title": sample("photo_lot_title"),
            "start_price": "0",
            "step": "1",
            "photos": (output, "input.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 201
    photos = json_object(json_response(res)["lot"])["photos"]
    assert isinstance(photos, list)
    photo_url = photos[0]
    assert isinstance(photo_url, str)
    photo = client.get(photo_url)
    assert photo.status_code == 200
    assert b"Exif\x00\x00junk" not in photo.data
    assert private_comment not in photo.data
    with Image.open(io.BytesIO(photo.data)) as normalized:
        assert "comment" not in normalized.info and "exif" not in normalized.info
    res = client.post(
        "/api/admin/lots",
        headers=auth(1),
        data={
            "operation_id": str(uuid4()),
            "title": sample("photo_lot_title"),
            "start_price": "0",
            "step": "1",
            "photos": (io.BytesIO(b"bad"), "x.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 400


def test_large_valid_photo_is_accepted_with_form_limits(client: FlaskClient) -> None:
    """Accept a valid photo larger than the default form-field size limit."""
    image = Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90)
    output.seek(0)
    res = client.post(
        "/api/admin/lots",
        headers=auth(1),
        data={
            "operation_id": str(uuid4()),
            "title": sample("photo_lot_title"),
            "start_price": "0",
            "step": "1",
            "photos": (output, "input.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 201


def test_failed_photo_write_rolls_back_database_and_partial_file(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove partial images and roll back the lot when a photo write fails."""
    image = Image.new("RGB", (15, 15), "red")
    output = io.BytesIO()
    image.save(output, "JPEG")
    output.seek(0)
    original_write = Path.write_bytes

    def fail_partial_write(path: Path, content: bytes) -> None:
        """Leave a partial photo on disk, then simulate storage failure."""
        original_write(path, content[:10])
        raise OSError("Simulated full disk")

    monkeypatch.setattr(Path, "write_bytes", fail_partial_write)
    with pytest.raises(OSError, match="Simulated full disk"):
        client.post(
            "/api/admin/lots",
            headers=auth(1),
            data={
                "operation_id": str(uuid4()),
                "title": "Test lot",
                "start_price": "0",
                "step": "1",
                "photos": (output, "input.jpg"),
            },
            content_type="multipart/form-data",
        )
    assert list(Path(app.config["UPLOADS"]).iterdir()) == []
    db = connect(app.config["DB"])
    try:
        assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM photos").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM lot_operations").fetchone()[0] == 0
    finally:
        db.close()


def test_digest_and_reminder_no_duplicate(app: Flask, client: FlaskClient) -> None:
    """Send each daily digest and closing reminder only once per recipient."""
    lot = create_lot(client)
    approve(client, 2)
    approve(client, 3)
    assert bid(client, lot, 2, 0, None).status_code == 200
    assert bid(client, lot, 3, 50, 0).status_code == 200
    now = datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc).timestamp()  # 18:00 Novosibirsk
    # Events are produced at real wall time; move them to before the test clock.
    db = connect(app.config["DB"])
    db.execute("UPDATE events SET created=?", (now - 10,))
    db.close()
    messages = []

    def sender(token: str, uid: int, message: str) -> bool:
        """Record notification recipients and text without contacting Telegram."""
        messages.append((uid, message))
        return True

    tick(app, now, sender)
    tick(app, now + 10, sender)
    assert len(messages) == 1 and messages[0][0] == 2
    app.config["END"] = now + 1800
    tick(app, now + 20, sender)
    tick(app, now + 30, sender)
    assert sum(sample("reminder_fragment") in message for _, message in messages) == 2


def test_retention_removes_data_and_files(app: Flask, client: FlaskClient) -> None:
    """Make expired data inaccessible and delete stored records and photographs."""
    create_lot(client)
    path = Path(app.config["UPLOADS"]) / "old.jpg"
    path.write_bytes(b"data")
    app.config["DELETE_AFTER"] = time.time() - 1
    assert client.get("/api/lots").status_code == 410
    tick(app)
    db = connect(app.config["DB"])
    assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    assert not path.exists()
    db.close()


def test_edit_only_before_first_bid_and_fallback(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock bid-bearing lots and advance declined candidates with explicit approval."""
    lot = create_lot(client)
    data = {
        "title": sample("edited_lot_title"),
        "description": sample("lot_description"),
        "condition": sample("edited_lot_condition"),
        "start_price": 0,
        "step": 50,
    }
    assert client.post(f"/api/admin/lots/{lot}/edit", headers=auth(2), json=data).status_code == 403
    assert client.post(f"/api/admin/lots/{lot}/edit", headers=auth(1), json=data).status_code == 200
    approve(client, 2)
    approve(client, 3)
    assert bid(client, lot, 2, 0, None, revision=2).status_code == 200
    assert client.post(f"/api/admin/lots/{lot}/edit", headers=auth(1), json=data).status_code == 409
    assert bid(client, lot, 3, 50, 0, revision=2).status_code == 200
    app.config["END"] = time.time() - 1
    assert (
        client.post(
            f"/api/admin/lots/{lot}", headers=auth(1), json={"action": "agreed"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/admin/lots/{lot}",
            headers=auth(1),
            json={"action": "refuse", "expected_candidate_id": 3},
        ).status_code
        == 200
    )
    result = json_response(client.get(f"/api/admin/lots/{lot}", headers=auth(1)))
    assert [item["id"] for item in json_objects(result["candidates"])] == [2]
    assert result["fulfillment"] == ""
    assert not json_object(json_response(client.get(f"/api/lots/{lot}", headers=auth(3)))["lot"])[
        "leading"
    ]
    fallback = json_object(json_response(client.get(f"/api/lots/{lot}", headers=auth(2)))["lot"])
    assert fallback["leading"] and fallback["price"] == 0 and fallback["count"] == 2
    assert [row["amount"] for row in json_objects(result["bids"])] == [50, 0]
    import auction.worker

    sent: list[tuple[int, str]] = []

    def sender(token: str, uid: int, message: str) -> bool:
        """Record candidate offers without allowing external Telegram delivery."""
        sent.append((uid, message))
        return True

    monkeypatch.setattr(auction.worker, "send_message", sender)
    assert (
        client.post(
            f"/api/admin/lots/{lot}/notify-candidate",
            headers=auth(2),
            json={"expected_candidate_id": 2},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/admin/lots/{lot}/notify-candidate",
            headers=auth(1),
            json={"expected_candidate_id": 3},
        ).status_code
        == 409
    )
    assert not sent
    assert (
        client.post(
            f"/api/admin/lots/{lot}/notify-candidate",
            headers=auth(1),
            json={"expected_candidate_id": 2},
        ).status_code
        == 200
    )
    assert sent[0][0] == 2
    assert (
        client.post(
            f"/api/admin/lots/{lot}",
            headers=auth(1),
            json={"action": "refuse", "expected_candidate_id": 2},
        ).status_code
        == 200
    )
    exhausted = json_object(json_response(client.get(f"/api/lots/{lot}", headers=auth(2)))["lot"])
    assert not exhausted["leading"] and exhausted["price"] is None and exhausted["count"] == 2


def test_concurrent_refusals_cannot_skip_next_candidate(app: Flask, client: FlaskClient) -> None:
    """Ensure duplicate concurrent refusals cannot decline a second participant."""
    lot = create_lot(client)
    approve(client, 2)
    approve(client, 3)
    assert bid(client, lot, 2, 0, None).status_code == 200
    assert bid(client, lot, 3, 50, 0).status_code == 200
    app.config["END"] = time.time() - 1
    assert (
        client.post(
            f"/api/admin/lots/{lot}", headers=auth(1), json={"action": "refuse"}
        ).status_code
        == 400
    )

    def refuse(_: int) -> int:
        """Decline the same candidate from an independent request context."""
        with app.test_client() as session:
            return session.post(
                f"/api/admin/lots/{lot}",
                headers=auth(1),
                json={"action": "refuse", "expected_candidate_id": 3},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(refuse, range(2)))
    assert sorted(results) == [200, 409]
    remaining = json_objects(
        json_response(client.get(f"/api/admin/lots/{lot}", headers=auth(1)))["candidates"]
    )
    assert [candidate["id"] for candidate in remaining] == [2]


def test_last_hour_outbid_sends_without_daily_delay(app: Flask, client: FlaskClient) -> None:
    """Deliver last-hour outbid events without waiting for the daily digest."""
    lot = create_lot(client)
    approve(client, 2)
    approve(client, 3)
    app.config["END"] = time.time() + 1800
    assert bid(client, lot, 2, 0, None).status_code == 200
    assert bid(client, lot, 3, 50, 0).status_code == 200
    sent: list[tuple[int, str]] = []

    def sender(token: str, uid: int, message: str) -> bool:
        """Record final-hour recipients and text in place of Telegram delivery."""
        sent.append((uid, message))
        return True

    tick(app, time.time(), sender)
    assert any(uid == 2 and sample("outbid_fragment") in message for uid, message in sent)


def test_public_access_and_explicit_participation_subscription(client: FlaskClient) -> None:
    """Allow public browsing and watching while reserving participation for bidders."""
    lot = create_lot(client)
    assert client.get("/api/lots").status_code == 200
    assert (
        client.post(
            f"/api/lots/{lot}/subscription",
            headers=auth(2),
            json={"kind": "participation", "enabled": True},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/lots/{lot}/subscription",
            headers=auth(2),
            json={"kind": "watching", "enabled": True},
        ).status_code
        == 200
    )


def test_application_requires_private_names_and_admin_only_visibility(client: FlaskClient) -> None:
    """Require both applicant names and expose them only to the organizer."""
    lot = create_lot(client)
    assert client.post("/api/application", headers=auth(2), json={}).status_code == 400
    assert (
        client.post(
            "/api/application",
            headers=auth(2),
            json={"given_name": sample("given_name"), "family_name": "  "},
        ).status_code
        == 400
    )
    assert (
        json_response(
            client.post(
                "/api/application",
                headers=auth(2),
                json={
                    "given_name": sample("padded_given_name"),
                    "family_name": sample("padded_family_name"),
                },
            )
        )["status"]
        == "pending"
    )
    assert (
        client.post("/api/admin/users/2", headers=auth(1), json={"status": "approved"}).status_code
        == 200
    )
    assert bid(client, lot, 2, 0, None).status_code == 200
    owner = json_objects(
        json_response(client.get("/api/admin/applications", headers=auth(1)))["users"]
    )[0]
    assert (owner["applicant_given_name"], owner["applicant_family_name"]) == (
        sample("given_name"),
        sample("private_family_name"),
    )
    assert json_objects(
        json_response(client.get(f"/api/admin/lots/{lot}", headers=auth(1)))["candidates"]
    )[0]["applicant_family_name"] == sample("private_family_name")
    for path, headers in [
        ("/api/lots", {}),
        (f"/api/lots/{lot}", auth(3)),
        ("/api/me", auth(3)),
        ("/api/my-bids", auth(3)),
    ]:
        assert sample("private_family_name") not in client.get(path, headers=headers).get_data(
            as_text=True
        )
    assert client.get("/api/admin/applications", headers=auth(3)).status_code == 403
    assert client.get(f"/api/admin/lots/{lot}", headers=auth(3)).status_code == 403


def test_existing_database_migrates_without_losing_applications(tmp_path: Path) -> None:
    """Add applicant-name fields without losing existing admission requests."""
    db_path = str(tmp_path / "old.sqlite3")
    db = connect(db_path)
    db.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new')"
    )
    db.execute("INSERT INTO users VALUES (2,'old','Friend','pending')")
    db.close()
    app = create_app(
        {
            "TESTING": True,
            "DB": db_path,
            "UPLOADS": str(tmp_path / "photos"),
            "BOT_TOKEN": TOKEN,
            "ADMIN_ID": "1",
        }
    )
    client = app.test_client()
    user = json_objects(
        json_response(client.get("/api/admin/applications", headers=auth(1)))["users"]
    )[0]
    assert user["id"] == 2 and user["status"] == "pending"
    assert user["applicant_given_name"] == user["applicant_family_name"] == ""


def test_bot_start_only_answers_private_request_and_persists_cursor(app: Flask) -> None:
    """Answer private start commands once and persist the Telegram update cursor."""
    from auction.worker import poll_start_messages

    requests: list[tuple[str, JSONObject]] = []
    incoming: list[JSONObject] = [
        {
            "update_id": 10,
            "message": {"chat": {"type": "private", "id": 2}, "from": {"id": 2}, "text": "/start"},
        },
        {
            "update_id": 11,
            "message": {"chat": {"type": "group", "id": 99}, "from": {"id": 2}, "text": "/start"},
        },
        {
            "update_id": 12,
            "message": {"chat": {"type": "private", "id": 3}, "from": {"id": 3}, "text": "hello"},
        },
    ]

    def fake(token: str, method: str, payload: JSONObject) -> JSONValue:
        """Emulate Telegram updates and delivery using only in-memory fixtures."""
        requests.append((method, payload))
        if method == "getUpdates":
            offset = json_integer(payload["offset"])
            return [update for update in incoming if json_integer(update["update_id"]) >= offset]
        return {"message_id": 1}

    poll_start_messages(app, "https://example.test", fake)
    sent = [p for method, p in requests if method == "sendMessage"]
    assert len(sent) == 1 and sent[0]["chat_id"] == 2
    keyboard = json_object(sent[0]["reply_markup"])["inline_keyboard"]
    assert isinstance(keyboard, list)
    button_row = keyboard[0]
    assert isinstance(button_row, list)
    web_app = json_object(json_object(button_row[0])["web_app"])
    assert web_app["url"] == "https://example.test"
    poll_start_messages(app, "https://example.test", fake)
    assert len([p for method, p in requests if method == "sendMessage"]) == 1
    assert requests[-1][1]["offset"] == 13


@pytest.mark.parametrize("retryable", [False, True])
def test_bot_start_delivery_failure_only_blocks_cursor_when_retryable(
    app: Flask, retryable: bool
) -> None:
    """Retry transient deliveries while advancing past permanent Telegram failures."""
    from auction.worker import poll_start_messages

    incoming: list[JSONObject] = [
        {
            "update_id": 10,
            "message": {"chat": {"type": "private", "id": 2}, "from": {"id": 2}, "text": "/start"},
        },
        {
            "update_id": 11,
            "message": {"chat": {"type": "private", "id": 3}, "from": {"id": 3}, "text": "/start"},
        },
    ]
    delivered: list[int] = []

    def fake(token: str, method: str, payload: JSONObject) -> JSONValue:
        """Emulate Telegram updates and delivery using only in-memory fixtures."""
        if method == "getUpdates":
            offset = json_integer(payload["offset"])
            return [update for update in incoming if json_integer(update["update_id"]) >= offset]
        if payload["chat_id"] == 2:
            raise TelegramError("Delivery failed", retryable=retryable)
        delivered.append(json_integer(payload["chat_id"]))
        return {"message_id": 1}

    poll_start_messages(app, "https://example.test", fake)
    db = connect(app.config["DB"])
    try:
        cursor = db.execute("SELECT completed FROM jobs WHERE name='telegram_offset'").fetchone()
    finally:
        db.close()
    if retryable:
        assert delivered == [] and cursor is None
    else:
        assert delivered == [3] and cursor["completed"] == 12


@pytest.mark.parametrize(("status", "retryable"), [(403, False), (429, True), (502, True)])
def test_telegram_http_errors_are_sanitized_and_classified(
    monkeypatch: pytest.MonkeyPatch, status: int, retryable: bool
) -> None:
    """Classify retryable HTTP failures without exposing tokens or response details."""
    secret = "fake-private-token"

    def reject(request: urllib.request.Request, timeout: float) -> NoReturn:
        """Raise an HTTP failure containing details that must remain private."""
        raise urllib.error.HTTPError(
            request.full_url, status, "private response", Message(), io.BytesIO(b"private details")
        )

    monkeypatch.setattr("auction.telegram.urllib.request.urlopen", reject)
    with pytest.raises(TelegramError) as caught:
        call(secret, "sendMessage", {"chat_id": 2, "text": "test"})
    assert caught.value.retryable is retryable
    assert secret not in str(caught.value) and "private" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_worker_stops_after_successful_retention_cleanup(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit the worker after cleanup rather than repeating an expired auction loop."""
    from auction import worker

    app.config["DELETE_AFTER"] = time.time() - 1
    monkeypatch.setattr(worker, "create_app", lambda: app)
    cleaned = []
    original_tick = worker.tick

    def cleanup(selected_app: Flask) -> bool | None:
        """Record the final retention pass while running the real cleanup logic."""
        cleaned.append(selected_app)
        return original_tick(selected_app)

    monkeypatch.setattr(worker, "tick", cleanup)
    monkeypatch.setattr(
        time, "sleep", lambda seconds: pytest.fail("Worker repeated completed cleanup")
    )
    worker.main()
    assert cleaned == [app]


def test_last_hour_notification_is_bounded_even_for_many_lots(app: Flask) -> None:
    """Bound aggregated Telegram text while acknowledging all included events."""
    now = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc).timestamp()
    app.config["END"] = now + 1800
    db = connect(app.config["DB"])
    try:
        db.execute("INSERT INTO users(id,first_name,status) VALUES(2,'Participant','approved')")
        for index in range(40):
            lot_id = db.execute(
                "INSERT INTO lots(title,description,condition,start_price,step,created) VALUES(?,?,?,?,?,?)",
                (str(index) + "x" * 115, "", "", 0, 1, now - 10),
            ).lastrowid
            db.execute(
                "INSERT INTO subscriptions(lot_id,user_id,participation) VALUES(?,2,1)", (lot_id,)
            )
            db.execute(
                "INSERT INTO events(lot_id,user_id,kind,created) VALUES(?,2,'outbid',?)",
                (lot_id, now - 5),
            )
    finally:
        db.close()
    messages: list[str] = []

    def sender(token: str, uid: int, message: str) -> bool:
        """Capture the aggregated text without a Telegram network request."""
        messages.append(message)
        return True

    tick(app, now, sender)
    assert len(messages) == 1 and len(messages[0]) <= 4096
    db = connect(app.config["DB"])
    try:
        assert db.execute("SELECT count(*) FROM events WHERE sent=0").fetchone()[0] == 0
    finally:
        db.close()


def test_trial_mode_is_explicit_and_does_not_enable_demo(app: Flask, client: FlaskClient) -> None:
    """Expose the trial marker while retaining Telegram authorization requirements."""
    app.config["TRIAL"] = True
    me = json_response(client.get("/api/me"))
    assert me["trial"] is True and me["demo"] is False and me["user"] is None
    assert (
        client.get("/api/admin/applications", headers={"X-Demo-User": "admin"}).status_code == 401
    )


def test_product_bid_history_is_complete_and_only_for_admin(client: FlaskClient) -> None:
    """Return the full named bid history exclusively to the organizer."""
    lot = create_lot(client)
    for uid, first, last in [
        (2, sample("given_name"), sample("family_name")),
        (
            3,
            sample("second_given_name"),
            sample("second_family_name"),
        ),
    ]:
        assert (
            client.post(
                "/api/application",
                headers=auth(uid),
                json={"given_name": first, "family_name": last},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/admin/users/{uid}", headers=auth(1), json={"status": "approved"}
            ).status_code
            == 200
        )
    assert bid(client, lot, 2, 0, None).status_code == 200
    assert bid(client, lot, 3, 50, 0).status_code == 200
    assert bid(client, lot, 2, 100, 50).status_code == 200
    history = json_objects(
        json_response(client.get(f"/api/admin/lots/{lot}", headers=auth(1)))["bids"]
    )
    assert [
        (b["amount"], b["applicant_given_name"], b["applicant_family_name"]) for b in history
    ] == [
        (100, sample("given_name"), sample("family_name")),
        (
            50,
            sample("second_given_name"),
            sample("second_family_name"),
        ),
        (0, sample("given_name"), sample("family_name")),
    ]
    assert all(isinstance(b["created"], (int, float)) for b in history)
    assert client.get(f"/api/admin/lots/{lot}").status_code == 401
    assert client.get(f"/api/admin/lots/{lot}", headers=auth(2)).status_code == 403
    assert client.get(f"/api/admin/lots/{lot}", headers=auth(3)).status_code == 403
    for headers in ({}, auth(2), auth(3)):
        public = client.get(f"/api/lots/{lot}", headers=headers).get_data(as_text=True)
        assert (
            sample("family_name") not in public
            and sample("second_family_name") not in public
            and "user_id" not in public
        )


def test_delete_lot_preserves_bids_and_photos_in_private_archive(
    app: Flask, client: FlaskClient
) -> None:
    """Hide an archived lot publicly while retaining accepted bids and organizer history."""
    lot = create_lot(client)
    approve(client, 2)
    approve(client, 3)
    assert bid(client, lot, 2, 0, None).status_code == 200
    assert bid(client, lot, 3, 50, 0).status_code == 200
    photo_id = "a" * 32
    photo_path = Path(app.config["UPLOADS"]) / "archive-photo.jpg"
    Image.new("RGB", (2, 2), "red").save(photo_path, "JPEG")
    db = connect(app.config["DB"])
    try:
        db.execute(
            "INSERT INTO photos(id,lot_id,filename) VALUES(?,?,?)", (photo_id, lot, photo_path.name)
        )
    finally:
        db.close()
    endpoint = f"/api/admin/lots/{lot}/delete"
    assert client.post(endpoint, json={"expected_bid_count": 2}).status_code == 401
    assert client.post(endpoint, headers=auth(2), json={"expected_bid_count": 2}).status_code == 403
    assert client.post(endpoint, headers=auth(1), json={}).status_code == 400
    assert client.post(endpoint, headers=auth(1), json={"expected_bid_count": 1}).status_code == 409
    assert client.get(f"/api/lots/{lot}").status_code == 200
    assert client.post(endpoint, headers=auth(1), json={"expected_bid_count": 2}).status_code == 200
    assert json_response(client.get("/api/lots"))["lots"] == []
    assert client.get(f"/api/lots/{lot}").status_code == 404
    assert client.get(f"/photos/{photo_id}").status_code == 404
    assert photo_path.exists()
    assert bid(client, lot, 2, 100, 50).status_code == 404
    assert (
        client.post(
            f"/api/lots/{lot}/subscription",
            headers=auth(2),
            json={"kind": "watching", "enabled": True},
        ).status_code
        == 404
    )
    mine = json_objects(json_response(client.get("/api/my-bids", headers=auth(2)))["bids"])
    assert [entry["amount"] for entry in mine] == [0]
    archived = json_response(client.get(f"/api/admin/lots/{lot}", headers=auth(1)))
    assert json_object(archived["lot"])["deleted"] is True
    assert [row["amount"] for row in json_objects(archived["bids"])] == [50, 0]
    assert client.get("/api/admin/lots").status_code == 401
    assert client.get("/api/admin/lots", headers=auth(2)).status_code == 403
    listed = json_objects(json_response(client.get("/api/admin/lots", headers=auth(1)))["lots"])
    assert len(listed) == 1 and listed[0]["deleted"] is True
    app.config["END"] = time.time() + 1800
    sent: list[str] = []

    def sender(token: str, uid: int, message: str) -> bool:
        """Capture unexpected notification attempts without contacting Telegram."""
        sent.append(message)
        return True

    tick(app, time.time(), sender)
    assert not sent


def test_delete_and_first_bid_are_serialized(app: Flask, client: FlaskClient) -> None:
    """Never accept a first bid and a deletion confirmed against zero bids together."""
    lot = create_lot(client)
    approve(client, 2)

    def mutate(action: int) -> int:
        """Submit one competing mutation through its own Flask request context."""
        with app.test_client() as session:
            if action == 0:
                return session.post(
                    f"/api/admin/lots/{lot}/delete", headers=auth(1), json={"expected_bid_count": 0}
                ).status_code
            return bid(session, lot, 2, 0, None).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        deleted, accepted = pool.map(mutate, [0, 1])
    assert (deleted, accepted) in ((200, 404), (409, 200))


def test_existing_lots_gain_archive_flag_without_losing_bids(
    app: Flask, client: FlaskClient
) -> None:
    """Upgrade the previous lot schema in place while preserving accepted auction records."""
    lot = create_lot(client)
    approve(client, 2)
    assert bid(client, lot, 2, 0, None).status_code == 200
    db = connect(app.config["DB"])
    try:
        db.execute("ALTER TABLE lots DROP COLUMN deleted")
    finally:
        db.close()
    updated = create_app(app.config)
    new_client = updated.test_client()
    assert json_object(json_response(new_client.get(f"/api/lots/{lot}"))["lot"])["count"] == 1
    archived = json_object(
        json_response(new_client.get(f"/api/admin/lots/{lot}", headers=auth(1)))["lot"]
    )
    assert archived["deleted"] is False


@pytest.mark.parametrize("path", ["/api/application", "/api/me"])
def test_expiry_cleanup_wins_before_delayed_account_write(
    app: Flask, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Reject a previously admitted request when cleanup precedes its account write."""
    before = time.time()
    clock = [before]
    app.config["DELETE_AFTER"] = before + 1
    headers = auth(444)
    admitted, resume = Event(), Event()

    @app.before_request
    def pause_admitted_request() -> None:
        """Pause after the real admission hook, before the account write lock."""
        admitted.set()
        assert resume.wait(10)

    def now() -> float:
        """Provide a boundary clock without waiting for the actual retention date."""
        return clock[0]

    def submit() -> int:
        """Issue the suspended request through its own Flask client context."""
        with app.test_client() as session:
            if path == "/api/me":
                return session.get(path, headers=headers).status_code
            return session.post(
                path,
                headers=headers,
                json={"given_name": sample("given_name"), "family_name": sample("family_name")},
            ).status_code

    monkeypatch.setattr("auction.app.time.time", now)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(submit)
        try:
            assert admitted.wait(5)
            clock[0] = before + 2
            assert tick(app, clock[0], lambda *_: False) is True
        finally:
            resume.set()
        assert future.result(timeout=5) == 410
    with closing(connect(app.config["DB"])) as db:
        for table in ("users", "jobs", "lot_operations", "candidate_notifications"):
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_expiry_cleanup_waits_for_earlier_account_transaction(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow an earlier write to finish, then remove it before final cleanup returns."""
    before = time.time()
    clock = [before]
    app.config["DELETE_AFTER"] = before + 1
    headers = auth(445)
    writing, resume = Event(), Event()

    def now() -> float:
        """Advance the synthetic clock only after the account lock is held."""
        return clock[0]

    def observed_connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
        """Observe a real request transaction without changing its SQL statements."""
        db = connect(path)

        def trace(statement: str) -> None:
            """Pause the insert while its write transaction blocks cleanup."""
            if statement.startswith("INSERT INTO users"):
                writing.set()
                assert resume.wait(10)

        db.set_trace_callback(trace)
        return db

    def submit() -> int:
        """Create a synthetic account through a separate request context."""
        with app.test_client() as session:
            return session.get("/api/me", headers=headers).status_code

    monkeypatch.setattr("auction.app.time.time", now)
    monkeypatch.setattr("auction.app.connect", observed_connect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        request_future = pool.submit(submit)
        assert writing.wait(5)
        clock[0] = before + 2
        cleanup_future = pool.submit(tick, app, clock[0], lambda *_: False)
        resume.set()
        assert request_future.result(timeout=5) == 200
        assert cleanup_future.result(timeout=5) is True
    with closing(connect(app.config["DB"])) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_expiry_blocks_application_after_account_refresh_completed(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recheck expiry in the application transaction after an earlier account refresh."""
    before = time.time()
    clock = [before]
    app.config["DELETE_AFTER"] = before + 1
    headers = auth(446)
    assert client.get("/api/me", headers=headers).status_code == 200
    parsing, resume = Event(), Event()
    original = Request.get_json

    def now() -> float:
        """Advance the boundary after account refresh has committed."""
        return clock[0]

    def delayed_json(
        request: Request, force: bool = False, silent: bool = False, cache: bool = True
    ) -> JSONValue:
        """Suspend ordinary JSON parsing between account refresh and the later write."""
        value: JSONValue = original(request, force=force, silent=silent, cache=cache)
        parsing.set()
        assert resume.wait(10)
        return value

    def submit() -> int:
        """Submit a new application through its own request connection."""
        with app.test_client() as session:
            return session.post(
                "/api/application",
                headers=headers,
                json={"given_name": sample("given_name"), "family_name": sample("family_name")},
            ).status_code

    monkeypatch.setattr("auction.app.time.time", now)
    monkeypatch.setattr(Request, "get_json", delayed_json)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(submit)
        try:
            assert parsing.wait(5)
            clock[0] = before + 2
            assert tick(app, clock[0], lambda *_: False) is True
        finally:
            resume.set()
        assert future.result(timeout=5) == 410
    with closing(connect(app.config["DB"])) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_revision_rejects_changed_first_bid_terms(client: FlaskClient) -> None:
    """Bind first-bid confirmation to the exact observed revision, including text edits."""
    lot = create_lot(client)
    approve(client, 2)
    assert (
        client.post(
            f"/api/lots/{lot}/bid",
            headers=auth(2),
            json={"amount": 0, "expected_price": None},
        ).status_code
        == 409
    )
    data = {
        "title": sample("edited_lot_title"),
        "description": sample("lot_description"),
        "condition": sample("edited_lot_condition"),
        "start_price": 0,
        "step": 50,
    }
    assert client.post(f"/api/admin/lots/{lot}/edit", headers=auth(1), json=data).status_code == 200
    assert bid(client, lot, 2, 0, None, revision=1).status_code == 409
    response = bid(client, lot, 2, 0, None, revision=2)
    assert response.status_code == 200
    assert json_object(json_response(response)["lot"])["revision"] == 2


def test_revision_edit_and_first_bid_are_serialized(app: Flask, client: FlaskClient) -> None:
    """Accept either the original bid or the edit, never an unconfirmed changed lot."""
    lot = create_lot(client)
    approve(client, 2)
    data = {
        "title": sample("edited_lot_title"),
        "description": "",
        "condition": "",
        "start_price": 0,
        "step": 50,
    }

    def mutate(edit: bool) -> int:
        """Compete for the same lot through independent request connections."""
        with app.test_client() as session:
            if edit:
                return session.post(
                    f"/api/admin/lots/{lot}/edit", headers=auth(1), json=data
                ).status_code
            return bid(session, lot, 2, 0, None, revision=1).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        edited, accepted = pool.map(mutate, [True, False])
    assert (edited, accepted) in ((200, 409), (409, 200))


@pytest.mark.parametrize("with_photo", [False, True])
def test_create_operation_is_idempotent_under_concurrent_retries(
    app: Flask, with_photo: bool
) -> None:
    """Keep one lot, receipt and photo set when the same operation is retried."""
    operation_id = str(uuid4())
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, "JPEG")
    photo_bytes = image.getvalue()

    def submit(_: int) -> int:
        """Reconstruct identical multipart bodies without reusing consumed streams."""
        data: dict[str, str | tuple[io.BytesIO, str]] = {
            "operation_id": operation_id,
            "title": sample("lot_title"),
            "start_price": "0",
            "step": "50",
        }
        if with_photo:
            data["photos"] = (io.BytesIO(photo_bytes), "input.jpg")
        with app.test_client() as session:
            result = session.post("/api/admin/lots", headers=auth(1), data=data)
            assert result.status_code == 201
            return json_integer(json_object(json_response(result)["lot"])["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, retried = pool.map(submit, range(2))
    assert first == retried
    app.config["END"] = time.time() - 1
    assert submit(2) == first
    with closing(connect(app.config["DB"])) as db:
        assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM lot_operations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM photos").fetchone()[0] == int(with_photo)
    assert len(list(Path(app.config["UPLOADS"]).glob("*.jpg"))) == int(with_photo)


def test_create_operation_rejects_uuid_reuse_with_changed_content(client: FlaskClient) -> None:
    """Reject a different logical payload instead of silently duplicating a UUID."""
    data = {
        "operation_id": str(uuid4()),
        "title": sample("lot_title"),
        "start_price": "0",
        "step": "50",
    }
    assert client.post("/api/admin/lots", headers=auth(1), data=data).status_code == 201
    data["title"] = sample("edited_lot_title")
    assert client.post("/api/admin/lots", headers=auth(1), data=data).status_code == 409
    for invalid in ("", "not-a-uuid"):
        data["operation_id"] = invalid
        assert client.post("/api/admin/lots", headers=auth(1), data=data).status_code == 400


def prepare_fallback(app: Flask, client: FlaskClient) -> int:
    """Create a closed lot whose original winner has explicitly declined."""
    lot = create_lot(client)
    approve(client, 2)
    approve(client, 3)
    assert bid(client, lot, 2, 0, None).status_code == 200
    assert bid(client, lot, 3, 50, 0).status_code == 200
    app.config["END"] = time.time() - 1
    assert (
        client.post(
            f"/api/admin/lots/{lot}",
            headers=auth(1),
            json={"action": "refuse", "expected_candidate_id": 3},
        ).status_code
        == 200
    )
    return lot


def test_candidate_send_reservation_blocks_concurrent_changes(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the candidate stable during delivery without holding the global DB lock."""
    lot = prepare_fallback(app, client)
    sending, resume = Event(), Event()
    delivered: list[int] = []

    def sender(token: str, user_id: int, message: str) -> bool:
        """Pause the simulated external effect so other API requests can compete."""
        sending.set()
        assert resume.wait(10)
        delivered.append(user_id)
        return True

    def notify() -> int:
        """Start one offer in an independent request context."""
        with app.test_client() as session:
            return session.post(
                f"/api/admin/lots/{lot}/notify-candidate",
                headers=auth(1),
                json={"expected_candidate_id": 2},
            ).status_code

    monkeypatch.setattr("auction.worker.send_message", sender)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(notify)
        try:
            assert sending.wait(5)
            for action in ("refuse", "cancel"):
                assert (
                    client.post(
                        f"/api/admin/lots/{lot}",
                        headers=auth(1),
                        json={"action": action, "expected_candidate_id": 2},
                    ).status_code
                    == 409
                )
            assert (
                client.post(
                    f"/api/admin/lots/{lot}/delete", headers=auth(1), json={"expected_bid_count": 2}
                ).status_code
                == 409
            )
            assert notify() == 409
            assert client.get("/api/me", headers=auth(2)).status_code == 200
        finally:
            resume.set()
        assert future.result(timeout=5) == 200
    assert notify() == 200 and delivered == [2]
    state = json_response(client.get(f"/api/admin/lots/{lot}", headers=auth(1)))[
        "candidate_notification"
    ]
    assert json_object(state) == {"candidate_id": 2, "state": "sent"}


def test_unknown_candidate_send_requires_manual_reconciliation(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve ambiguity, block blind retries, and permit only explicit resolution."""
    lot = prepare_fallback(app, client)
    attempts: list[int] = []

    def uncertain(token: str, user_id: int, message: str) -> bool:
        """Simulate an unacknowledged send, which may already have reached Telegram."""
        attempts.append(user_id)
        return False

    monkeypatch.setattr("auction.worker.send_message", uncertain)
    endpoint = f"/api/admin/lots/{lot}/notify-candidate"
    for _ in range(2):
        assert (
            client.post(endpoint, headers=auth(1), json={"expected_candidate_id": 2}).status_code
            == 409
        )
    assert attempts == [2]
    assert (
        client.post(
            f"/api/admin/lots/{lot}", headers=auth(1), json={"action": "cancel"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/admin/lots/{lot}/delete", headers=auth(1), json={"expected_bid_count": 2}
        ).status_code
        == 409
    )
    resolve = f"/api/admin/lots/{lot}/candidate-notification/resolve"
    assert (
        client.post(
            resolve, headers=auth(2), json={"expected_candidate_id": 2, "outcome": "sent"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            resolve, headers=auth(1), json={"expected_candidate_id": 3, "outcome": "sent"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            resolve, headers=auth(1), json={"expected_candidate_id": 2, "outcome": "invalid"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            resolve, headers=auth(1), json={"expected_candidate_id": 2, "outcome": "not_sent"}
        ).status_code
        == 200
    )
    assert (
        client.post(endpoint, headers=auth(1), json={"expected_candidate_id": 2}).status_code == 409
    )
    assert attempts == [2, 2]
    assert (
        client.post(
            resolve, headers=auth(1), json={"expected_candidate_id": 2, "outcome": "sent"}
        ).status_code
        == 200
    )
    assert (
        client.post(endpoint, headers=auth(1), json={"expected_candidate_id": 2}).status_code == 200
    )
    assert attempts == [2, 2]


@pytest.mark.parametrize("action", ["refuse", "cancel", "delete"])
def test_candidate_change_before_send_rejects_stale_offer(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Reject an outdated offer when candidate change acquires the write lock first."""
    lot = prepare_fallback(app, client)
    attempts: list[int] = []

    def forbidden(token: str, user_id: int, message: str) -> NoReturn:
        """Ensure a stale operation never crosses the simulated network boundary."""
        attempts.append(user_id)
        raise AssertionError("Unexpected Telegram send")

    monkeypatch.setattr("auction.worker.send_message", forbidden)
    if action == "delete":
        changed = client.post(
            f"/api/admin/lots/{lot}/delete", headers=auth(1), json={"expected_bid_count": 2}
        )
    else:
        changed = client.post(
            f"/api/admin/lots/{lot}",
            headers=auth(1),
            json={"action": action, "expected_candidate_id": 2},
        )
    assert changed.status_code == 200
    sent = client.post(
        f"/api/admin/lots/{lot}/notify-candidate",
        headers=auth(1),
        json={"expected_candidate_id": 2},
    )
    assert sent.status_code == (404 if action == "delete" else 409)
    assert attempts == []


def test_revision_migration_preserves_lots_and_bids(app: Flask, client: FlaskClient) -> None:
    """Add revision to the previous schema without dropping auction history."""
    lot = create_lot(client)
    approve(client, 2)
    assert bid(client, lot, 2, 0, None).status_code == 200
    with closing(connect(app.config["DB"])) as db:
        db.execute("ALTER TABLE lots DROP COLUMN revision")
    migrated = create_app(app.config)
    result = json_object(json_response(migrated.test_client().get(f"/api/lots/{lot}"))["lot"])
    assert result["revision"] == 1 and result["count"] == 1 and result["price"] == 0


def test_stale_candidate_send_lease_recovers_without_automatic_retry(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turn an abandoned sending reservation into unknown after its bounded lease."""
    lot = prepare_fallback(app, client)
    attempts: list[int] = []
    with closing(connect(app.config["DB"])) as db:
        db.execute(
            "INSERT INTO candidate_notifications(lot_id,candidate_id,attempt_id,state,updated) VALUES(?,2,'old-attempt','sending',?)",
            (lot, time.time() - 121),
        )

    def forbidden(token: str, user_id: int, message: str) -> NoReturn:
        """Fail if merely reading or retrying an unknown offer attempts delivery."""
        attempts.append(user_id)
        raise AssertionError("Unexpected Telegram send")

    monkeypatch.setattr("auction.worker.send_message", forbidden)
    result = json_response(client.get(f"/api/admin/lots/{lot}", headers=auth(1)))
    assert json_object(result["candidate_notification"])["state"] == "unknown"
    assert (
        client.post(
            f"/api/admin/lots/{lot}/notify-candidate",
            headers=auth(1),
            json={"expected_candidate_id": 2},
        ).status_code
        == 409
    )
    assert attempts == []
    app.config["DELETE_AFTER"] = time.time() - 1
    assert tick(app) is True
    with closing(connect(app.config["DB"])) as db:
        for table in ("candidate_notifications", "lot_operations", "users", "lots", "jobs"):
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_personal_history_carries_safe_archived_status(app: Flask, client: FlaskClient) -> None:
    """Report the caller's archived status without exposing refusal IDs or other names."""
    lot = prepare_fallback(app, client)
    mine = json_objects(json_response(client.get("/api/my-bids", headers=auth(2)))["bids"])[0]
    assert mine["status"] == "ended" and mine["own_leading"] is True
    assert (
        client.post(
            f"/api/admin/lots/{lot}/delete", headers=auth(1), json={"expected_bid_count": 2}
        ).status_code
        == 200
    )
    mine = json_objects(json_response(client.get("/api/my-bids", headers=auth(2)))["bids"])[0]
    assert (
        mine["deleted"] is True and mine["status"] == "cancelled" and mine["own_leading"] is False
    )
    assert set(mine) == {
        "id",
        "amount",
        "created",
        "lot_id",
        "title",
        "deleted",
        "status",
        "own_leading",
    }


def test_worker_heartbeat_tracks_progress_and_stops_after_expiry(app: Flask) -> None:
    """Record health progress while running and leave no receipt after final cleanup."""
    assert tick(app) is None
    with closing(connect(app.config["DB"])) as db:
        assert db.execute("SELECT completed FROM jobs WHERE name='worker_heartbeat'").fetchone()
    app.config["DELETE_AFTER"] = time.time() - 1
    assert tick(app) is True
    from auction.worker import record_heartbeat

    record_heartbeat(app)
    with closing(connect(app.config["DB"])) as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_public_photo_limits_are_separate_from_admin_api(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound image requests without allowing public flooding to exhaust admin buckets."""
    monkeypatch.setattr("auction.app.time.monotonic", lambda: 600.0)
    lot = create_lot(client)
    image = Path(app.config["UPLOADS"]) / "public.jpg"
    Image.new("RGB", (2, 2), "red").save(image, "JPEG")
    with closing(connect(app.config["DB"])) as db:
        db.execute(
            "INSERT INTO photos(id,lot_id,filename) VALUES('public-photo',?,'public.jpg')", (lot,)
        )
    for _ in range(600):
        assert (
            client.get(
                "/photos/public-photo", headers={"X-Forwarded-For": str(uuid4())}
            ).status_code
            == 200
        )
    rejected = client.get("/photos/public-photo")
    assert rejected.status_code == 429 and rejected.headers["Retry-After"] == "60"
    assert client.get("/api/admin/lots", headers=auth(1)).status_code == 200
    app.config["CLOUDFLARE_PROXY"] = True
    assert (
        client.get("/photos/public-photo", headers={"CF-Connecting-IP": "192.0.2.11"}).status_code
        == 200
    )
    assert (
        client.get("/photos/public-photo", headers={"CF-Connecting-IP": "192.0.2.12"}).status_code
        == 200
    )
    app.config["CLOUDFLARE_PROXY"] = False
    assert (
        client.get("/photos/public-photo", headers={"CF-Connecting-IP": "192.0.2.13"}).status_code
        == 429
    )
