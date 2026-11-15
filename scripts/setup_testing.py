#!/usr/bin/env python3
"""Configure a dedicated integration-test bot without printing its token."""

import getpass
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from auction.i18n import translate
from auction.telegram import TelegramError, call

CONFIG = ROOT / "data" / "testing-config.json"


def main():
    os.umask(0o077)
    print(translate("setup_intro"))
    if CONFIG.exists():
        print(translate("setup_configuration_exists"))
        return 1
    token = getpass.getpass(translate("setup_token_prompt")).strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        print(translate("setup_token_invalid"))
        return 1
    try:
        bot = call(token, "getMe")
        webhook = call(token, "getWebhookInfo")
    except TelegramError as error:
        print(str(error))
        return 1
    username = bot.get("username", "") if isinstance(bot, dict) else ""
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        print(translate("setup_username_unavailable"))
        return 1
    if webhook.get("url"):
        print(translate("setup_webhook_conflict"))
        return 1
    print(translate("setup_bot_label", username))
    print(translate("setup_start_instructions", username))
    input(translate("setup_continue_prompt"))
    try:
        updates = call(token, "getUpdates", {"limit": 100, "timeout": 0})
    except TelegramError as error:
        print(str(error))
        return 1
    people = {}
    for update in updates if isinstance(updates, list) else []:
        message = update.get("message") or {}
        person = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text") or ""
        if (
            chat.get("type") == "private"
            and type(person.get("id")) is int
            and chat.get("id") == person["id"]
            and not person.get("is_bot")
            and (text == "/start" or text.startswith("/start "))
        ):
            people[person["id"]] = person
    if not people:
        print(translate("setup_start_not_found"))
        return 1
    users = list(people.values())
    print(translate("setup_account_selection"))
    for i, person in enumerate(users, 1):
        name = " ".join(str(person.get(k) or "") for k in ("first_name", "last_name")).strip()
        label = (
            "@" + person["username"]
            if person.get("username")
            else translate("setup_without_username")
        )
        print(f"{i}. {name} ({label}, ID {person['id']})")
    if len(users) == 1:
        person = users[0]
    else:
        try:
            selected = int(input(translate("setup_selection_prompt")).strip())
            if selected < 1 or selected > len(users):
                raise ValueError
        except ValueError:
            print(translate("setup_selection_invalid"))
            return 1
        person = users[selected - 1]
    if input(
        translate("setup_account_confirmation", person.get("first_name", ""), person["id"])
    ).strip().lower() not in (translate("setup_yes_short"), translate("setup_yes"), "y", "yes"):
        print(translate("setup_cancelled"))
        return 1
    data = {"token": token, "admin_id": person["id"], "username": username}
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
    except OSError:
        print(translate("setup_save_failed"))
        return 1
    print(translate("setup_complete"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print(translate("setup_interrupted"))
        raise SystemExit(1)
