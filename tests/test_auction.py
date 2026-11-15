import hashlib
import hmac
import io
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from auction.app import connect, create_app, verify_telegram
from auction.telegram import TelegramError, call
from auction.worker import tick

TOKEN = "123456:fake-test-token"
SAMPLES = json.loads((Path(__file__).parent / "fixtures/messages.json").read_text(encoding="utf-8"))


def sample(key):
    return SAMPLES[key]


def signed(user_id, name=sample("participant_name"), username="friend", date=None):
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


def auth(user_id):
    return {"X-Telegram-Init-Data": signed(user_id, username=f"u{user_id}")}


@pytest.fixture
def app(tmp_path):
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
def client(app):
    return app.test_client()


def test_initialization_closes_bootstrap_connection(tmp_path, monkeypatch):
    connections = []

    def observed_connect(path):
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


def create_lot(client, start=0, step=50):
    res = client.post(
        "/api/admin/lots",
        headers=auth(1),
        data={
            "title": sample("lot_title"),
            "description": sample("lot_description"),
            "condition": sample("lot_condition"),
            "start_price": str(start),
            "step": str(step),
        },
    )
    assert res.status_code == 201, res.data
    return res.json["lot"]["id"]


def approve(client, uid):
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


def bid(client, lot, uid, amount, expected):
    return client.post(
        f"/api/lots/{lot}/bid",
        headers=auth(uid),
        json={"amount": amount, "expected_price": expected},
    )


def test_telegram_signature_and_expiry():
    raw = signed(42)
    assert verify_telegram(raw, TOKEN)["id"] == 42
    assert verify_telegram(raw.replace("test", "evil"), TOKEN) is None
    assert verify_telegram(signed(42, date=time.time() - 86401), TOKEN) is None
    assert verify_telegram(raw, "wrong") is None
    assert verify_telegram(raw + "&hash=x", TOKEN) is None
    assert verify_telegram("auth_date=1&hash=%D0%B6", TOKEN) is None
    assert verify_telegram("auth_date=1&hash=" + "x" * 64, TOKEN) is None


def test_anonymous_flood_does_not_block_administrator(app, client, monkeypatch):
    monkeypatch.setattr("auction.app.time.monotonic", lambda: 6000)
    statuses = [client.post("/api/application", json={}).status_code for _ in range(61)]
    assert statuses[:60] == [401] * 60
    assert statuses[-1] == 429
    assert client.get("/api/admin/applications", headers=auth(1)).status_code == 200


def test_one_signed_user_cannot_exhaust_another_users_write_limit(app, client, monkeypatch):
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


def test_trial_application_flood_is_limited_per_cloudflare_client(app, client, monkeypatch):
    monkeypatch.setattr("auction.app.time.monotonic", lambda: 6000)
    app.config["TRIAL"] = True
    payload = {
        "given_name": sample("given_name"),
        "family_name": sample("family_name"),
    }

    def headers(uid, ip):
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


def test_oversized_lot_id_is_not_a_server_error(client):
    assert client.get("/api/lots/" + "9" * 30).status_code == 404


def test_zero_bid_step_approval_and_private_data(client):
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
    mine = client.get("/api/my-bids", headers=auth(2)).json["bids"]
    assert [b["amount"] for b in mine] == [0]
    assert client.get(f"/api/lots/{lot}", headers=auth(3)).json["lot"]["leading"]


def test_concurrent_bids_only_one_wins(app, client):
    lot = create_lot(client, start=100)
    approve(client, 2)
    approve(client, 3)

    def run(uid):
        with app.test_client() as c:
            return bid(c, lot, uid, 100, None).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [2, 3]))
    assert sorted(results) == [200, 409]
    assert client.get(f"/api/lots/{lot}").json["lot"]["count"] == 1


def test_close_is_strict_and_no_late_bid(app, client):
    lot = create_lot(client)
    approve(client, 2)
    app.config["END"] = time.time() - 1
    assert bid(client, lot, 2, 0, None).status_code == 409
    assert client.get(f"/api/lots/{lot}").json["lot"]["status"] == "ended"


def test_bid_cannot_cross_deadline_during_request(app, client, monkeypatch):
    lot = create_lot(client)
    approve(client, 2)
    headers = auth(2)
    before = time.time()
    app.config["END"] = before + 1
    calls = []

    def clock():
        calls.append(None)
        # Expiry guard, authentication, and first bid check all precede close.
        return before if len(calls) <= 3 else app.config["END"]

    monkeypatch.setattr("auction.app.time.time", clock)
    response = client.post(
        f"/api/lots/{lot}/bid", headers=headers, json={"amount": 0, "expected_price": None}
    )
    assert response.status_code == 409
    db = connect(app.config["DB"])
    try:
        assert db.execute("SELECT count(*) FROM bids").fetchone()[0] == 0
    finally:
        db.close()


def test_lot_creation_rechecks_deadline_after_preparation(app, client, monkeypatch):
    headers = auth(1)
    before = time.time()
    app.config["END"] = before + 1
    calls = []

    def clock():
        calls.append(None)
        return before if len(calls) <= 3 else app.config["END"]

    monkeypatch.setattr("auction.app.time.time", clock)
    response = client.post(
        "/api/admin/lots",
        headers=headers,
        data={"title": "Test lot", "start_price": "0", "step": "1"},
    )
    assert response.status_code == 409
    db = connect(app.config["DB"])
    try:
        assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 0
    finally:
        db.close()


def test_photo_is_reencoded_and_rejects_fake_image(app, client):
    image = Image.new("RGB", (15, 15), "red")
    output = io.BytesIO()
    image.save(output, "JPEG", exif=b"Exif\x00\x00junk")
    output.seek(0)
    res = client.post(
        "/api/admin/lots",
        headers=auth(1),
        data={
            "title": sample("photo_lot_title"),
            "start_price": "0",
            "step": "1",
            "photos": (output, "input.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 201
    photo = client.get(res.json["lot"]["photos"][0])
    assert photo.status_code == 200
    assert b"Exif\x00\x00junk" not in photo.data
    res = client.post(
        "/api/admin/lots",
        headers=auth(1),
        data={
            "title": sample("photo_lot_title"),
            "start_price": "0",
            "step": "1",
            "photos": (io.BytesIO(b"bad"), "x.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 400


def test_large_valid_photo_is_accepted_with_form_limits(client):
    image = Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90)
    output.seek(0)
    res = client.post(
        "/api/admin/lots",
        headers=auth(1),
        data={
            "title": sample("photo_lot_title"),
            "start_price": "0",
            "step": "1",
            "photos": (output, "input.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 201


def test_failed_photo_write_rolls_back_database_and_partial_file(app, client, monkeypatch):
    image = Image.new("RGB", (15, 15), "red")
    output = io.BytesIO()
    image.save(output, "JPEG")
    output.seek(0)
    original_write = Path.write_bytes

    def fail_partial_write(path, content):
        original_write(path, content[:10])
        raise OSError("Simulated full disk")

    monkeypatch.setattr(Path, "write_bytes", fail_partial_write)
    with pytest.raises(OSError, match="Simulated full disk"):
        client.post(
            "/api/admin/lots",
            headers=auth(1),
            data={
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
    finally:
        db.close()


def test_digest_and_reminder_no_duplicate(app, client):
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

    def sender(token, uid, message):
        messages.append((uid, message))
        return True

    tick(app, now, sender)
    tick(app, now + 10, sender)
    assert len(messages) == 1 and messages[0][0] == 2
    app.config["END"] = now + 1800
    tick(app, now + 20, sender)
    tick(app, now + 30, sender)
    assert sum(sample("reminder_fragment") in message for _, message in messages) == 2


def test_retention_removes_data_and_files(app, client):
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


def test_edit_only_before_first_bid_and_fallback(app, client, monkeypatch):
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
    assert bid(client, lot, 2, 0, None).status_code == 200
    assert client.post(f"/api/admin/lots/{lot}/edit", headers=auth(1), json=data).status_code == 409
    assert bid(client, lot, 3, 50, 0).status_code == 200
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
    result = client.get(f"/api/admin/lots/{lot}", headers=auth(1)).json
    assert [item["id"] for item in result["candidates"]] == [2]
    assert result["fulfillment"] == ""
    assert not client.get(f"/api/lots/{lot}", headers=auth(3)).json["lot"]["leading"]
    fallback = client.get(f"/api/lots/{lot}", headers=auth(2)).json["lot"]
    assert fallback["leading"] and fallback["price"] == 0 and fallback["count"] == 2
    assert [row["amount"] for row in result["bids"]] == [50, 0]
    import auction.worker

    sent = []
    monkeypatch.setattr(
        auction.worker,
        "send_message",
        lambda token, uid, message: sent.append((uid, message)) or True,
    )
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
    exhausted = client.get(f"/api/lots/{lot}", headers=auth(2)).json["lot"]
    assert not exhausted["leading"] and exhausted["price"] is None and exhausted["count"] == 2


def test_concurrent_refusals_cannot_skip_next_candidate(app, client):
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

    def refuse(_):
        with app.test_client() as session:
            return session.post(
                f"/api/admin/lots/{lot}",
                headers=auth(1),
                json={"action": "refuse", "expected_candidate_id": 3},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(refuse, range(2)))
    assert sorted(results) == [200, 409]
    remaining = client.get(f"/api/admin/lots/{lot}", headers=auth(1)).json["candidates"]
    assert [candidate["id"] for candidate in remaining] == [2]


def test_last_hour_outbid_sends_without_daily_delay(app, client):
    lot = create_lot(client)
    approve(client, 2)
    approve(client, 3)
    app.config["END"] = time.time() + 1800
    assert bid(client, lot, 2, 0, None).status_code == 200
    assert bid(client, lot, 3, 50, 0).status_code == 200
    sent = []
    tick(app, time.time(), lambda token, uid, message: sent.append((uid, message)) or True)
    assert any(uid == 2 and sample("outbid_fragment") in message for uid, message in sent)


def test_public_access_and_explicit_participation_subscription(client):
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


def test_application_requires_private_names_and_admin_only_visibility(client):
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
        client.post(
            "/api/application",
            headers=auth(2),
            json={
                "given_name": sample("padded_given_name"),
                "family_name": sample("padded_family_name"),
            },
        ).json["status"]
        == "pending"
    )
    assert (
        client.post("/api/admin/users/2", headers=auth(1), json={"status": "approved"}).status_code
        == 200
    )
    assert bid(client, lot, 2, 0, None).status_code == 200
    owner = client.get("/api/admin/applications", headers=auth(1)).json["users"][0]
    assert (owner["applicant_given_name"], owner["applicant_family_name"]) == (
        sample("given_name"),
        sample("private_family_name"),
    )
    assert client.get(f"/api/admin/lots/{lot}", headers=auth(1)).json["candidates"][0][
        "applicant_family_name"
    ] == sample("private_family_name")
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


def test_existing_database_migrates_without_losing_applications(tmp_path):
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
    user = client.get("/api/admin/applications", headers=auth(1)).json["users"][0]
    assert user["id"] == 2 and user["status"] == "pending"
    assert user["applicant_given_name"] == user["applicant_family_name"] == ""


def test_bot_start_only_answers_private_request_and_persists_cursor(app):
    from auction.worker import poll_start_messages

    requests = []
    incoming = [
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

    def fake(token, method, payload):
        requests.append((method, payload))
        if method == "getUpdates":
            return [u for u in incoming if u["update_id"] >= payload["offset"]]
        return {"message_id": 1}

    poll_start_messages(app, "https://example.test", fake)
    sent = [p for method, p in requests if method == "sendMessage"]
    assert len(sent) == 1 and sent[0]["chat_id"] == 2
    assert (
        sent[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == "https://example.test"
    )
    poll_start_messages(app, "https://example.test", fake)
    assert len([p for method, p in requests if method == "sendMessage"]) == 1
    assert requests[-1][1]["offset"] == 13


@pytest.mark.parametrize("retryable", [False, True])
def test_bot_start_delivery_failure_only_blocks_cursor_when_retryable(app, retryable):
    from auction.worker import poll_start_messages

    incoming = [
        {
            "update_id": 10,
            "message": {"chat": {"type": "private", "id": 2}, "from": {"id": 2}, "text": "/start"},
        },
        {
            "update_id": 11,
            "message": {"chat": {"type": "private", "id": 3}, "from": {"id": 3}, "text": "/start"},
        },
    ]
    delivered = []

    def fake(token, method, payload):
        if method == "getUpdates":
            return [update for update in incoming if update["update_id"] >= payload["offset"]]
        if payload["chat_id"] == 2:
            raise TelegramError("Delivery failed", retryable=retryable)
        delivered.append(payload["chat_id"])
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
def test_telegram_http_errors_are_sanitized_and_classified(monkeypatch, status, retryable):
    secret = "fake-private-token"

    def reject(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, status, "private response", {}, io.BytesIO(b"private details")
        )

    monkeypatch.setattr("auction.telegram.urllib.request.urlopen", reject)
    with pytest.raises(TelegramError) as caught:
        call(secret, "sendMessage", {"chat_id": 2, "text": "test"})
    assert caught.value.retryable is retryable
    assert secret not in str(caught.value) and "private" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_worker_stops_after_successful_retention_cleanup(app, monkeypatch):
    from auction import worker

    app.config["DELETE_AFTER"] = time.time() - 1
    monkeypatch.setattr(worker, "create_app", lambda: app)
    cleaned = []
    original_tick = worker.tick

    def cleanup(selected_app):
        cleaned.append(selected_app)
        return original_tick(selected_app)

    monkeypatch.setattr(worker, "tick", cleanup)
    monkeypatch.setattr(
        worker.time, "sleep", lambda seconds: pytest.fail("Worker repeated completed cleanup")
    )
    worker.main()
    assert cleaned == [app]


def test_last_hour_notification_is_bounded_even_for_many_lots(app):
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
    messages = []
    tick(app, now, lambda token, uid, message: messages.append(message) or True)
    assert len(messages) == 1 and len(messages[0]) <= 4096
    db = connect(app.config["DB"])
    try:
        assert db.execute("SELECT count(*) FROM events WHERE sent=0").fetchone()[0] == 0
    finally:
        db.close()


def test_trial_mode_is_explicit_and_does_not_enable_demo(app, client):
    app.config["TRIAL"] = True
    me = client.get("/api/me").json
    assert me["trial"] is True and me["demo"] is False and me["user"] is None
    assert (
        client.get("/api/admin/applications", headers={"X-Demo-User": "admin"}).status_code == 401
    )


def test_product_bid_history_is_complete_and_only_for_admin(client):
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
    history = client.get(f"/api/admin/lots/{lot}", headers=auth(1)).json["bids"]
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
