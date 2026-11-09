import hashlib
import hmac
import io
import json
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from PIL import Image
from auction.app import create_app, connect, verify_telegram
from auction.worker import tick
from auction.i18n import translate

TOKEN = '123456:fake-test-token'


def signed(user_id, name=translate('fixture_signed_01'), username='friend', date=None):
    data = {'auth_date': str(int(time.time() if date is None else date)), 'query_id': 'test',
            'user': json.dumps({'id': user_id, 'first_name': name, 'username': username}, ensure_ascii=False, separators=(',', ':'))}
    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def auth(user_id):
    return {'X-Telegram-Init-Data': signed(user_id, username=f'u{user_id}')}


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DB': str(tmp_path/'db.sqlite3'), 'UPLOADS': str(tmp_path/'photos'),
                       'BOT_TOKEN': TOKEN, 'ADMIN_ID': '1', 'END': time.time()+36000, 'DELETE_AFTER': time.time()+86400})


@pytest.fixture
def client(app):
    return app.test_client()


def create_lot(client, start=0, step=50):
    res = client.post('/api/admin/lots', headers=auth(1), data={'title': translate('fixture_create_lot_01'), 'description': translate('fixture_create_lot_02'), 'condition': translate('fixture_create_lot_03'), 'start_price': str(start), 'step': str(step)})
    assert res.status_code == 201, res.data
    return res.json['lot']['id']


def approve(client, uid):
    assert client.post('/api/application', headers=auth(uid), json={'given_name':translate('fixture_approve_01'), 'family_name':translate('fixture_approve_02')}).status_code == 200
    assert client.post(f'/api/admin/users/{uid}', headers=auth(1), json={'status': 'approved'}).status_code == 200


def bid(client, lot, uid, amount, expected):
    return client.post(f'/api/lots/{lot}/bid', headers=auth(uid), json={'amount': amount, 'expected_price': expected})


def test_telegram_signature_and_expiry():
    raw = signed(42)
    assert verify_telegram(raw, TOKEN)['id'] == 42
    assert verify_telegram(raw.replace('test', 'evil'), TOKEN) is None
    assert verify_telegram(signed(42, date=time.time()-86401), TOKEN) is None
    assert verify_telegram(raw, 'wrong') is None
    assert verify_telegram(raw + '&hash=x', TOKEN) is None
    assert verify_telegram('auth_date=1&hash=%D0%B6', TOKEN) is None
    assert verify_telegram('auth_date=1&hash=' + 'x' * 64, TOKEN) is None


def test_anonymous_flood_does_not_block_administrator(app, client, monkeypatch):
    monkeypatch.setattr('auction.app.time.monotonic', lambda: 6000)
    statuses = [client.post('/api/application', json={}).status_code for _ in range(61)]
    assert statuses[:60] == [401] * 60
    assert statuses[-1] == 429
    assert client.get('/api/admin/applications', headers=auth(1)).status_code == 200


def test_one_signed_user_cannot_exhaust_another_users_write_limit(app, client, monkeypatch):
    monkeypatch.setattr('auction.app.time.monotonic', lambda: 6000)
    lot = create_lot(client)
    payload = {'kind':'watching', 'enabled':True}
    statuses = [client.post(f'/api/lots/{lot}/subscription', headers=auth(2), json=payload).status_code for _ in range(91)]
    assert statuses[:90] == [200] * 90
    assert statuses[-1] == 429
    assert client.post(f'/api/lots/{lot}/subscription', headers=auth(3), json=payload).status_code == 200


def test_trial_application_flood_is_limited_per_cloudflare_client(app, client, monkeypatch):
    monkeypatch.setattr('auction.app.time.monotonic', lambda: 6000)
    app.config['TRIAL'] = True
    payload = {'given_name':translate('fixture_approve_01'), 'family_name':translate('fixture_approve_02')}
    def headers(uid, ip):
        return {**auth(uid), 'CF-Connecting-IP':ip}
    assert all(client.post('/api/application', headers=headers(uid,'203.0.113.8'), json=payload).status_code == 200 for uid in range(2,22))
    assert client.post('/api/application', headers=headers(22,'203.0.113.8'), json=payload).status_code == 429
    assert client.post('/api/application', headers=headers(23,'203.0.113.9'), json=payload).status_code == 200
    assert client.get('/api/admin/applications', headers=headers(1,'203.0.113.8')).status_code == 200


def test_oversized_lot_id_is_not_a_server_error(client):
    assert client.get('/api/lots/' + '9' * 30).status_code == 404


def test_zero_bid_step_approval_and_private_data(client):
    lot = create_lot(client)
    assert bid(client, lot, 2, 0, None).status_code == 403
    approve(client, 2)
    assert bid(client, lot, 2, 0, None).status_code == 200
    approve(client, 3)
    assert bid(client, lot, 3, 25, 0).status_code == 400
    assert bid(client, lot, 3, 100, 0).status_code == 200
    assert bid(client, lot, 2, 150, 0).status_code == 409
    public = client.get(f'/api/lots/{lot}').get_data(as_text=True)
    assert 'user_id' not in public and 'u2' not in public and 'u3' not in public
    assert client.get('/api/admin/applications', headers=auth(2)).status_code == 403
    assert client.get(f'/api/admin/lots/{lot}', headers=auth(3)).status_code == 403
    mine = client.get('/api/my-bids', headers=auth(2)).json['bids']
    assert [b['amount'] for b in mine] == [0]
    assert client.get(f'/api/lots/{lot}', headers=auth(3)).json['lot']['leading']


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
    assert client.get(f'/api/lots/{lot}').json['lot']['count'] == 1


def test_close_is_strict_and_no_late_bid(app, client):
    lot = create_lot(client)
    approve(client, 2)
    app.config['END'] = time.time() - 1
    assert bid(client, lot, 2, 0, None).status_code == 409
    assert client.get(f'/api/lots/{lot}').json['lot']['status'] == 'ended'


def test_photo_is_reencoded_and_rejects_fake_image(app, client):
    image = Image.new('RGB', (15, 15), 'red')
    output = io.BytesIO(); image.save(output, 'JPEG', exif=b'Exif\x00\x00junk'); output.seek(0)
    res = client.post('/api/admin/lots', headers=auth(1), data={'title':translate('fixture_test_photo_is_reencoded_and_rejects_fake_image_01'),'start_price':'0','step':'1','photos':(output,'input.jpg')}, content_type='multipart/form-data')
    assert res.status_code == 201
    photo = client.get(res.json['lot']['photos'][0])
    assert photo.status_code == 200
    assert b'Exif\x00\x00junk' not in photo.data
    res = client.post('/api/admin/lots', headers=auth(1), data={'title':translate('fixture_test_photo_is_reencoded_and_rejects_fake_image_01'),'start_price':'0','step':'1','photos':(io.BytesIO(b'bad'),'x.jpg')}, content_type='multipart/form-data')
    assert res.status_code == 400


def test_large_valid_photo_is_accepted_with_form_limits(client):
    image = Image.frombytes('RGB', (600, 600), os.urandom(600 * 600 * 3))
    output = io.BytesIO(); image.save(output, 'JPEG', quality=90); output.seek(0)
    res = client.post('/api/admin/lots', headers=auth(1), data={'title':translate('fixture_test_photo_is_reencoded_and_rejects_fake_image_01'), 'start_price':'0', 'step':'1', 'photos':(output,'input.jpg')}, content_type='multipart/form-data')
    assert res.status_code == 201


def test_digest_and_reminder_no_duplicate(app, client):
    lot = create_lot(client)
    approve(client,2); approve(client,3)
    assert bid(client,lot,2,0,None).status_code == 200
    assert bid(client,lot,3,50,0).status_code == 200
    now = datetime(2026,11,6,11,0,tzinfo=timezone.utc).timestamp()  # 18:00 Novosibirsk
    # Events are produced at real wall time; move them to before the test clock.
    db=connect(app.config['DB']);db.execute('UPDATE events SET created=?',(now-10,));db.close()
    messages=[]
    sender=lambda token, uid, message: messages.append((uid,message)) or True
    tick(app, now, sender);tick(app, now+10, sender)
    assert len(messages) == 1 and messages[0][0] == 2
    app.config['END'] = now + 1800
    tick(app, now+20, sender);tick(app, now+30, sender)
    assert sum(translate('fixture_test_digest_and_reminder_no_duplicate_01') in message for _,message in messages) == 2


def test_retention_removes_data_and_files(app, client):
    lot=create_lot(client)
    path=__import__('pathlib').Path(app.config['UPLOADS'])/'old.jpg';path.write_bytes(b'data')
    app.config['DELETE_AFTER']=time.time()-1
    assert client.get('/api/lots').status_code == 410
    tick(app)
    db=connect(app.config['DB'])
    assert db.execute('SELECT count(*) FROM lots').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM users').fetchone()[0] == 0
    assert not path.exists()
    db.close()


def test_edit_only_before_first_bid_and_fallback(app, client, monkeypatch):
    lot = create_lot(client)
    data = {'title':translate('fixture_test_edit_only_before_first_bid_and_fallback_01'), 'description':translate('fixture_create_lot_02'), 'condition':translate('fixture_test_edit_only_before_first_bid_and_fallback_02'), 'start_price':0, 'step':50}
    assert client.post(f'/api/admin/lots/{lot}/edit', headers=auth(2), json=data).status_code == 403
    assert client.post(f'/api/admin/lots/{lot}/edit', headers=auth(1), json=data).status_code == 200
    approve(client,2); approve(client,3)
    assert bid(client,lot,2,0,None).status_code == 200
    assert client.post(f'/api/admin/lots/{lot}/edit', headers=auth(1), json=data).status_code == 409
    assert bid(client,lot,3,50,0).status_code == 200
    app.config['END']=time.time()-1
    assert client.post(f'/api/admin/lots/{lot}', headers=auth(1), json={'action':'refuse'}).status_code == 200
    result=client.get(f'/api/admin/lots/{lot}', headers=auth(1)).json
    assert [item['id'] for item in result['candidates']] == [2]
    assert not client.get(f'/api/lots/{lot}',headers=auth(3)).json['lot']['leading']
    import auction.worker
    sent=[]
    monkeypatch.setattr(auction.worker, 'send_message', lambda token,uid,message: sent.append((uid,message)) or True)
    assert client.post(f'/api/admin/lots/{lot}/notify-candidate', headers=auth(2)).status_code == 403
    assert client.post(f'/api/admin/lots/{lot}/notify-candidate', headers=auth(1)).status_code == 200
    assert sent[0][0] == 2


def test_last_hour_outbid_sends_without_daily_delay(app, client):
    lot=create_lot(client)
    approve(client,2); approve(client,3)
    app.config['END']=time.time()+1800
    assert bid(client,lot,2,0,None).status_code == 200
    assert bid(client,lot,3,50,0).status_code == 200
    sent=[]
    tick(app, time.time(), lambda token,uid,message: sent.append((uid,message)) or True)
    assert any(uid == 2 and translate('fixture_test_last_hour_outbid_sends_without_daily_delay_01') in message for uid,message in sent)


def test_public_access_and_explicit_participation_subscription(client):
    lot=create_lot(client)
    assert client.get('/api/lots').status_code == 200
    assert client.post(f'/api/lots/{lot}/subscription',headers=auth(2),json={'kind':'participation','enabled':True}).status_code == 400
    assert client.post(f'/api/lots/{lot}/subscription',headers=auth(2),json={'kind':'watching','enabled':True}).status_code == 200


def test_application_requires_private_names_and_admin_only_visibility(client):
    lot = create_lot(client)
    assert client.post('/api/application', headers=auth(2), json={}).status_code == 400
    assert client.post('/api/application', headers=auth(2), json={'given_name':translate('fixture_approve_01'), 'family_name':'  '}).status_code == 400
    assert client.post('/api/application', headers=auth(2), json={'given_name':translate('fixture_test_application_requires_private_names_and_admin_only_visibility_01'), 'family_name':translate('fixture_test_application_requires_private_names_and_admin_only_visibility_02')}).json['status'] == 'pending'
    assert client.post('/api/admin/users/2', headers=auth(1), json={'status':'approved'}).status_code == 200
    assert bid(client, lot, 2, 0, None).status_code == 200
    owner = client.get('/api/admin/applications', headers=auth(1)).json['users'][0]
    assert (owner['applicant_given_name'], owner['applicant_family_name']) == (translate('fixture_approve_01'), translate('fixture_test_application_requires_private_names_and_admin_only_visibility_03'))
    assert client.get(f'/api/admin/lots/{lot}', headers=auth(1)).json['candidates'][0]['applicant_family_name'] == translate('fixture_test_application_requires_private_names_and_admin_only_visibility_03')
    for path, headers in [('/api/lots', {}), (f'/api/lots/{lot}', auth(3)), ('/api/me', auth(3)), ('/api/my-bids', auth(3))]:
        assert translate('fixture_test_application_requires_private_names_and_admin_only_visibility_03') not in client.get(path, headers=headers).get_data(as_text=True)
    assert client.get('/api/admin/applications', headers=auth(3)).status_code == 403
    assert client.get(f'/api/admin/lots/{lot}', headers=auth(3)).status_code == 403


def test_existing_database_migrates_without_losing_applications(tmp_path):
    db_path = str(tmp_path/'old.sqlite3')
    db = connect(db_path)
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new')")
    db.execute("INSERT INTO users VALUES (2,'old','Friend','pending')")
    db.close()
    app = create_app({'TESTING':True,'DB':db_path,'UPLOADS':str(tmp_path/'photos'),'BOT_TOKEN':TOKEN,'ADMIN_ID':'1'})
    client = app.test_client()
    user = client.get('/api/admin/applications', headers=auth(1)).json['users'][0]
    assert user['id'] == 2 and user['status'] == 'pending'
    assert user['applicant_given_name'] == user['applicant_family_name'] == ''


def test_bot_start_only_answers_private_request_and_persists_cursor(app):
    from auction.worker import poll_start_messages
    requests = []
    incoming = [
        {'update_id':10, 'message':{'chat':{'type':'private','id':2},'from':{'id':2},'text':'/start'}},
        {'update_id':11, 'message':{'chat':{'type':'group','id':99},'from':{'id':2},'text':'/start'}},
        {'update_id':12, 'message':{'chat':{'type':'private','id':3},'from':{'id':3},'text':'hello'}},
    ]
    def fake(token, method, payload):
        requests.append((method,payload))
        if method == 'getUpdates':
            return [u for u in incoming if u['update_id'] >= payload['offset']]
        return {'message_id':1}
    poll_start_messages(app, 'https://example.test', fake)
    sent = [p for method,p in requests if method == 'sendMessage']
    assert len(sent) == 1 and sent[0]['chat_id'] == 2
    assert sent[0]['reply_markup']['inline_keyboard'][0][0]['web_app']['url'] == 'https://example.test'
    poll_start_messages(app, 'https://example.test', fake)
    assert len([p for method,p in requests if method == 'sendMessage']) == 1
    assert requests[-1][1]['offset'] == 13


def test_trial_mode_is_explicit_and_does_not_enable_demo(app, client):
    app.config['TRIAL'] = True
    me = client.get('/api/me').json
    assert me['trial'] is True and me['demo'] is False and me['user'] is None
    assert client.get('/api/admin/applications', headers={'X-Demo-User':'admin'}).status_code == 401


def test_product_bid_history_is_complete_and_only_for_admin(client):
    lot = create_lot(client)
    for uid, first, last in [(2, translate('fixture_approve_01'), translate('fixture_approve_02')), (3, translate('fixture_test_product_bid_history_is_complete_and_only_for_admin_01'), translate('fixture_test_product_bid_history_is_complete_and_only_for_admin_02'))]:
        assert client.post('/api/application', headers=auth(uid), json={'given_name':first,'family_name':last}).status_code == 200
        assert client.post(f'/api/admin/users/{uid}', headers=auth(1), json={'status':'approved'}).status_code == 200
    assert bid(client,lot,2,0,None).status_code == 200
    assert bid(client,lot,3,50,0).status_code == 200
    assert bid(client,lot,2,100,50).status_code == 200
    history=client.get(f'/api/admin/lots/{lot}',headers=auth(1)).json['bids']
    assert [(b['amount'],b['applicant_given_name'],b['applicant_family_name']) for b in history] == [
        (100,translate('fixture_approve_01'),translate('fixture_approve_02')), (50,translate('fixture_test_product_bid_history_is_complete_and_only_for_admin_01'),translate('fixture_test_product_bid_history_is_complete_and_only_for_admin_02')), (0,translate('fixture_approve_01'),translate('fixture_approve_02'))]
    assert all(isinstance(b['created'],(int,float)) for b in history)
    assert client.get(f'/api/admin/lots/{lot}').status_code == 401
    assert client.get(f'/api/admin/lots/{lot}',headers=auth(2)).status_code == 403
    assert client.get(f'/api/admin/lots/{lot}',headers=auth(3)).status_code == 403
    for headers in ({}, auth(2), auth(3)):
        public=client.get(f'/api/lots/{lot}',headers=headers).get_data(as_text=True)
        assert translate('fixture_approve_02') not in public and translate('fixture_test_product_bid_history_is_complete_and_only_for_admin_02') not in public and 'user_id' not in public
