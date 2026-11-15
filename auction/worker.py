"""Separately run delivery worker; never starts on import.

Successful sends are recorded locally. A crash after Telegram accepts a message
but before that record is saved can cause duplicate delivery on restart.
"""

import fcntl
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeAlias, cast

from flask import Flask

from auction.app import connect, create_app
from auction.i18n import translate
from auction.telegram import TelegramCaller, TelegramError, call

TZ = timezone(timedelta(hours=7))
MessageSender: TypeAlias = Callable[[str, int, str], bool]


def send_message(token: str, user_id: int, message: str) -> bool:
    """Deliver one plain-text message, returning whether Telegram accepted it.

    Missing credentials and sanitized API failures return ``False`` so the
    worker can retry without marking the associated local event as delivered.
    """
    if not token:
        return False
    try:
        call(token, "sendMessage", {"chat_id": user_id, "text": message})
        return True
    except TelegramError:
        logging.warning("Telegram message delivery failed")
        return False


def poll_start_messages(app: Flask, app_url: str, send: TelegramCaller = call) -> None:
    """Reply to private ``/start`` requests and persist the Telegram update cursor.

    ``send`` implements the Bot API call contract and may be replaced by an
    offline adapter. A retryable reply failure leaves that update unacknowledged;
    non-retryable failures advance the cursor. Non-HTTPS app URLs are ignored.
    """
    if not app_url.startswith("https://"):
        return
    db = connect(cast(str, app.config["DB"]))
    token = cast(str, app.config["BOT_TOKEN"])
    try:
        row = db.execute("SELECT completed FROM jobs WHERE name='telegram_offset'").fetchone()
        offset = int(row["completed"]) if row else 0
        try:
            updates = send(
                token,
                "getUpdates",
                {"offset": offset, "timeout": 0, "limit": 100},
            )
        except TelegramError:
            logging.warning("Could not poll Telegram updates")
            return
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if type(update_id) is not int:
                continue
            message = update.get("message")
            if not isinstance(message, dict):
                message = {}
            chat = message.get("chat")
            if not isinstance(chat, dict):
                chat = {}
            person = message.get("from")
            if not isinstance(person, dict):
                person = {}
            text = message.get("text")
            chat_id = chat.get("id")
            if (
                chat.get("type") == "private"
                and type(chat_id) is int
                and chat_id == person.get("id")
                and isinstance(text, str)
                and (text == "/start" or text.startswith("/start "))
            ):
                try:
                    send(
                        token,
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": translate("bot_open_prompt"),
                            "reply_markup": {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": translate("bot_open_button"),
                                            "web_app": {"url": app_url},
                                        }
                                    ]
                                ]
                            },
                        },
                    )
                except TelegramError as error:
                    logging.warning("Could not reply to a Telegram start request")
                    if error.retryable:
                        break
            db.execute(
                "INSERT INTO jobs(name,completed) VALUES('telegram_offset',?) ON CONFLICT(name) DO UPDATE SET completed=excluded.completed",
                (update_id + 1,),
            )
    finally:
        db.close()


def tick(app: Flask, now: float | None = None, send: MessageSender = send_message) -> bool | None:
    """Process one retention and notification batch using the configured storage.

    ``now`` supplies a Unix timestamp for deterministic checks; otherwise wall
    time is used. ``send`` returns acceptance for each attempted message, and
    only accepted deliveries receive local receipts. Return ``True`` after
    retention cleanup and ``None`` while ordinary polling should continue.
    A crash between delivery and its receipt can cause duplicate messages.
    """
    now = time.time() if now is None else now
    db = connect(cast(str, app.config["DB"]))
    try:
        if now >= cast(float, app.config["DELETE_AFTER"]):
            # There are no user-created directories in UPLOADS. Repeat cleanup on restart.
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            for table in ("events", "subscriptions", "bids", "photos", "lots", "users", "jobs"):
                db.execute("DELETE FROM " + table)
            db.commit()
            for file in Path(cast(str, app.config["UPLOADS"])).glob("*.jpg"):
                file.unlink(missing_ok=True)
            db.execute("VACUUM")
            return True
        local = datetime.fromtimestamp(now, TZ)
        end = cast(float, app.config["END"])
        token = cast(str, app.config["BOT_TOKEN"])
        # Starting at 18:00, retry today's outstanding messages until the next day.
        if local.hour >= 18:
            date = local.date().isoformat()
            for user in db.execute(
                "SELECT DISTINCT user_id FROM events WHERE sent=0 AND created<=?", (now,)
            ).fetchall():
                job = f"digest:{date}:{user[0]}"
                if db.execute("SELECT 1 FROM jobs WHERE name=?", (job,)).fetchone():
                    continue
                rows = db.execute(
                    """SELECT e.id,e.lot_id,e.kind,l.title FROM events e JOIN lots l ON l.id=e.lot_id AND l.deleted=0
                  JOIN subscriptions s ON s.lot_id=e.lot_id AND s.user_id=e.user_id
                  WHERE e.user_id=? AND e.sent=0 AND e.created<=? AND ((e.kind='watch' AND s.watching=1) OR (e.kind='outbid' AND s.participation=1))""",
                    (user[0], now),
                ).fetchall()
                if not rows:
                    continue
                lots = list(dict.fromkeys((r["lot_id"], r["title"]) for r in rows))
                lines = []
                for lot_id, title in lots:
                    lead = db.execute(
                        "SELECT amount,user_id FROM bids WHERE lot_id=? ORDER BY amount DESC,id DESC LIMIT 1",
                        (lot_id,),
                    ).fetchone()
                    current = f"{lead['amount']} ₽" if lead else translate("notification_no_bids")
                    own = db.execute(
                        "SELECT 1 FROM bids WHERE lot_id=? AND user_id=? LIMIT 1", (lot_id, user[0])
                    ).fetchone()
                    status = (
                        (
                            translate("notification_leading_suffix")
                            if lead and lead["user_id"] == user[0]
                            else translate("notification_outbid_suffix")
                        )
                        if own
                        else ""
                    )
                    lines.append(f"• {title}: {current}{status}")
                body = "\n".join(lines)
                if len(body) > 3200:
                    body = body[:3000] + translate("notification_more_lots")
                msg = (
                    translate("notification_digest_prefix")
                    + body
                    + translate("notification_digest_suffix")
                )
                if send(token, user[0], msg):
                    db.execute("BEGIN IMMEDIATE")
                    db.executemany(
                        "UPDATE events SET sent=1 WHERE id=?", [(r["id"],) for r in rows]
                    )
                    db.execute("INSERT OR IGNORE INTO jobs(name,completed) VALUES(?,?)", (job, now))
                    db.commit()
        # Record one successful reminder per participant; never send it after close.
        if end - 3600 <= now < end:
            users = db.execute(
                "SELECT DISTINCT s.user_id FROM subscriptions s JOIN lots l ON l.id=s.lot_id WHERE s.participation=1 AND l.status='active' AND EXISTS (SELECT 1 FROM bids b WHERE b.user_id=s.user_id AND b.lot_id=s.lot_id)"
            ).fetchall()
            for user in users:
                job = f"reminder:{user[0]}"
                if db.execute("SELECT 1 FROM jobs WHERE name=?", (job,)).fetchone():
                    continue
                if send(token, user[0], translate("notification_closing_reminder")):
                    db.execute("INSERT OR IGNORE INTO jobs(name,completed) VALUES(?,?)", (job, now))
            users = db.execute(
                "SELECT DISTINCT user_id FROM events WHERE sent=0 AND kind='outbid' AND created>=?",
                (end - 3600,),
            ).fetchall()
            for user in users:
                rows = db.execute(
                    """SELECT e.id,l.title FROM events e JOIN lots l ON l.id=e.lot_id AND l.deleted=0 JOIN subscriptions s ON s.lot_id=e.lot_id AND s.user_id=e.user_id
                    WHERE e.user_id=? AND e.sent=0 AND e.kind='outbid' AND e.created>=? AND s.participation=1""",
                    (user[0], end - 3600),
                ).fetchall()
                if not rows:
                    continue
                body = ", ".join(dict.fromkeys(row["title"] for row in rows))
                if len(body) > 3200:
                    body = body[:3000] + translate("notification_more_lots")
                msg = (
                    translate("notification_outbid_prefix")
                    + body
                    + translate("notification_closing_suffix")
                )
                if send(token, user[0], msg):
                    db.executemany(
                        "UPDATE events SET sent=1 WHERE id=?", [(r["id"],) for r in rows]
                    )
    finally:
        db.close()
    return None


def main() -> None:
    """Hold the data-directory worker lock and process jobs until retention ends.

    Demo mode and a second worker for the same directory exit immediately.
    Iteration failures are logged and retried; expired data is cleaned before
    any Telegram polling, after which this worker exits successfully.
    """
    app = create_app()
    if app.config["DEMO"]:
        raise SystemExit("The notification worker cannot run in demo mode.")
    lock_path = Path(cast(str, app.config["DB"])).parent / "worker.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Worker already running.")
        last_tick = 0.0
        while True:
            try:
                if time.time() - last_tick >= 20:
                    if tick(app):
                        return
                    last_tick = time.time()
                if os.environ.get("AUCTION_BOT_POLL") == "1" and time.time() < cast(
                    float, app.config["DELETE_AFTER"]
                ):
                    poll_start_messages(app, os.environ.get("AUCTION_APP_URL", ""))
            except Exception:
                logging.exception("Worker iteration failed")
            time.sleep(5)


if __name__ == "__main__":
    main()
