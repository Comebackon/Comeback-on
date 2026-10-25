import json
from unittest.mock import patch
import pytest
import storage
import push_service

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'push.db')
    storage.initialize(path)
    connection = storage.connect(path)
    with connection:
        connection.execute("INSERT INTO users VALUES('u','e','n','hash','{}','now')")
    yield connection
    connection.close()

def test_keys_persist_and_private_key_is_not_public(db):
    first = push_service.keys(db)
    second = push_service.keys(db)
    assert first['public_key'] == second['public_key']
    assert first['private_key'] != first['public_key']

@pytest.mark.parametrize('url', ['http://fcm.googleapis.com/a','https://127.0.0.1/a','https://fcm.googleapis.com.evil.test/a','https://fcm.googleapis.com:444/a','https://evil.test/a'])
def test_untrusted_endpoints_rejected(url):
    with pytest.raises(ValueError):
        push_service.validate({'endpoint':url})

def test_send_expired_subscription_removed_and_failure_isolated(db):
    key = push_service.keys(db)
    sub = {'endpoint':'https://fcm.googleapis.com/example','keys':{'p256dh':key['public_key'],'auth':push_service.encoded(b'1234567890123456')}}
    with db:
        db.execute('INSERT INTO push_subscriptions VALUES(?,?,?)', (sub['endpoint'],'u',json.dumps(sub)))
    response = type('Response', (), {'status_code':410})()
    with patch('push_service.webpush', side_effect=push_service.WebPushException('gone', response=response)):
        assert push_service.send(db,'u',key['private_key']) == 0
    assert db.execute('SELECT count(*) FROM push_subscriptions').fetchone()[0] == 0

def test_api_auth_csrf_subscription_and_logout(tmp_path):
    from webapp import create_app
    app = create_app({'TESTING':True,'DATABASE':str(tmp_path/'api.db'),'DATA_DIR':str(tmp_path),'PHOTO_DIR':str(tmp_path/'photos'),'SECRET_KEY':'test','SESSION_COOKIE_SECURE':False})
    client = app.test_client()
    assert client.get('/api/push/key').status_code == 401
    result = client.post('/api/auth/register', json={'email':'push@example.test','password':'synthetic-password','nickname':'test'})
    assert result.status_code == 201
    headers = {'X-CSRF-Token':result.json['csrf_token']}
    key = client.get('/api/push/key').json
    assert set(key) == {'public_key'}
    sub = {'endpoint':'https://fcm.googleapis.com/test','keys':{'p256dh':key['public_key'],'auth':push_service.encoded(b'1234567890123456')}}
    assert client.post('/api/push/subscription',json=sub).status_code == 403
    assert client.post('/api/push/subscription',json=sub,headers=headers).status_code == 200
    with patch('push_service.webpush') as send:
        assert client.post('/api/push/test',json={},headers=headers).json['accepted'] == 1
        assert send.call_count == 1
    assert client.post('/api/auth/logout',json={},headers=headers).status_code == 200
    connection = storage.connect(app.config['DATABASE'])
    assert connection.execute('SELECT count(*) FROM push_subscriptions').fetchone()[0] == 0
    connection.close()
