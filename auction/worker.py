"""One optional, separately run delivery worker; safe to restart, never starts on import."""
import fcntl
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auction.app import create_app, connect
from auction.telegram import call, TelegramError

TZ = timezone(timedelta(hours=7))


def send_message(token, user_id, message):
    if not token:
        return False
    try:
        call(token, 'sendMessage', {'chat_id':user_id, 'text':message})
        return True
    except TelegramError:
        logging.warning('Telegram delivery failed; message remains queued')
        return False


def poll_start_messages(app, app_url, send=call):
    """Reply only to private /start requests; preserve the update cursor across restarts."""
    if not app_url.startswith('https://'):
        return
    db = connect(app.config['DB'])
    try:
        row = db.execute("SELECT completed FROM jobs WHERE name='telegram_offset'").fetchone()
        offset = int(row['completed']) if row else 0
        try:
            updates = send(app.config['BOT_TOKEN'], 'getUpdates', {'offset':offset, 'timeout':0, 'limit':100})
        except TelegramError:
            logging.warning('Could not poll Telegram updates')
            return
        for update in updates if isinstance(updates, list) else []:
            update_id = update.get('update_id')
            if type(update_id) is not int:
                continue
            message = update.get('message') or {}
            chat = message.get('chat') or {}
            person = message.get('from') or {}
            text = message.get('text') or ''
            if chat.get('type') == 'private' and type(chat.get('id')) is int and chat.get('id') == person.get('id') and (text == '/start' or text.startswith('/start ')):
                try:
                    send(app.config['BOT_TOKEN'], 'sendMessage', {
                        'chat_id': chat['id'], 'text':'Откройте аукцион кнопкой ниже.',
                        'reply_markup':{'inline_keyboard':[[{'text':'Открыть аукцион','web_app':{'url':app_url}}]]},
                    })
                except TelegramError:
                    logging.warning('Could not reply to a Telegram start request')
                    break
            db.execute("INSERT INTO jobs(name,completed) VALUES('telegram_offset',?) ON CONFLICT(name) DO UPDATE SET completed=excluded.completed", (update_id+1,))
    finally:
        db.close()


def tick(app, now=None, send=send_message):
    now = time.time() if now is None else now
    db = connect(app.config['DB'])
    try:
        if now >= app.config['DELETE_AFTER']:
            # There are no user-created directories in UPLOADS. Repeat cleanup on restart.
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            for table in ('events', 'subscriptions', 'bids', 'photos', 'lots', 'users', 'jobs'):
                db.execute('DELETE FROM ' + table)
            db.commit()
            for file in Path(app.config['UPLOADS']).glob('*.jpg'):
                file.unlink(missing_ok=True)
            db.execute('VACUUM')
            return
        local = datetime.fromtimestamp(now, TZ)
        end = app.config['END']
        token = app.config['BOT_TOKEN']
        # Starting at 18:00, retry today's outstanding messages until the next day.
        if local.hour >= 18:
            date = local.date().isoformat()
            for user in db.execute('SELECT DISTINCT user_id FROM events WHERE sent=0 AND created<=?', (now,)).fetchall():
                job = f'digest:{date}:{user[0]}'
                if db.execute('SELECT 1 FROM jobs WHERE name=?', (job,)).fetchone():
                    continue
                rows = db.execute('''SELECT e.id,e.lot_id,e.kind,l.title FROM events e JOIN lots l ON l.id=e.lot_id
                  JOIN subscriptions s ON s.lot_id=e.lot_id AND s.user_id=e.user_id
                  WHERE e.user_id=? AND e.sent=0 AND e.created<=? AND ((e.kind='watch' AND s.watching=1) OR (e.kind='outbid' AND s.participation=1))''', (user[0],now)).fetchall()
                if not rows:
                    continue
                lots = list(dict.fromkeys((r['lot_id'], r['title']) for r in rows))
                lines = []
                for lot_id, title in lots:
                    lead = db.execute('SELECT amount,user_id FROM bids WHERE lot_id=? ORDER BY amount DESC,id DESC LIMIT 1', (lot_id,)).fetchone()
                    current = f'{lead["amount"]} ₽' if lead else 'нет ставок'
                    own = db.execute('SELECT 1 FROM bids WHERE lot_id=? AND user_id=? LIMIT 1', (lot_id,user[0])).fetchone()
                    status = (' · вы лидируете' if lead and lead['user_id']==user[0] else ' · вашу ставку перебили') if own else ''
                    lines.append(f'• {title}: {current}{status}')
                body = '\n'.join(lines)
                if len(body) > 3200:
                    body = body[:3000] + '\n…остальные лоты в приложении.'
                msg = 'Новости аукциона:\n' + body + '\nОткройте приложение, чтобы увидеть подробности.'
                if send(token,user[0],msg):
                    db.execute('BEGIN IMMEDIATE')
                    db.executemany('UPDATE events SET sent=1 WHERE id=?', [(r['id'],) for r in rows])
                    db.execute('INSERT OR IGNORE INTO jobs(name,completed) VALUES(?,?)', (job,now))
                    db.commit()
        # Exactly one combined reminder per participating user, no reminder after close.
        if end - 3600 <= now < end:
            users = db.execute("SELECT DISTINCT s.user_id FROM subscriptions s JOIN lots l ON l.id=s.lot_id WHERE s.participation=1 AND l.status='active' AND EXISTS (SELECT 1 FROM bids b WHERE b.user_id=s.user_id AND b.lot_id=s.lot_id)").fetchall()
            for user in users:
                job = f'reminder:{user[0]}'
                if db.execute('SELECT 1 FROM jobs WHERE name=?', (job,)).fetchone():
                    continue
                if send(token,user[0],'До завершения аукциона осталось меньше часа. Проверьте свои лоты и ставки в приложении.'):
                    db.execute('INSERT OR IGNORE INTO jobs(name,completed) VALUES(?,?)', (job,now))
            users = db.execute("SELECT DISTINCT user_id FROM events WHERE sent=0 AND kind='outbid' AND created>=?", (end-3600,)).fetchall()
            for user in users:
                rows = db.execute("""SELECT e.id,l.title FROM events e JOIN lots l ON l.id=e.lot_id JOIN subscriptions s ON s.lot_id=e.lot_id AND s.user_id=e.user_id
                    WHERE e.user_id=? AND e.sent=0 AND e.kind='outbid' AND e.created>=? AND s.participation=1""", (user[0],end-3600)).fetchall()
                if not rows:
                    continue
                msg = 'Вашу ставку перебили: ' + ', '.join(dict.fromkeys(row['title'] for row in rows)) + '. Торги скоро завершатся — откройте приложение.'
                if send(token,user[0],msg):
                    db.executemany('UPDATE events SET sent=1 WHERE id=?', [(r['id'],) for r in rows])
    finally:
        db.close()


def main():
    app = create_app()
    if app.config['DEMO']:
        raise SystemExit('The notification worker cannot run in demo mode.')
    lock_path = Path(app.config['DB']).parent / 'worker.lock'
    with open(lock_path, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Worker already running.')
        last_tick = 0
        while True:
            try:
                if time.time() - last_tick >= 20:
                    tick(app)
                    last_tick = time.time()
                if os.environ.get('AUCTION_BOT_POLL') == '1' and time.time() < app.config['DELETE_AFTER']:
                    poll_start_messages(app, os.environ.get('AUCTION_APP_URL', ''))
            except Exception:
                logging.exception('Worker iteration failed')
            time.sleep(5)


if __name__ == '__main__':
    main()
