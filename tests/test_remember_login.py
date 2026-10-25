from pathlib import Path
import time
import pytest
import storage
from webapp import create_app


def make_app(tmp_path):
    return create_app({'TESTING': True, 'DATA_DIR':str(tmp_path), 'DATABASE':str(tmp_path/'db.sqlite'), 'PHOTO_DIR':str(tmp_path/'photos'), 'SESSION_COOKIE_SECURE':False})

@pytest.mark.parametrize('remember', [True, False])
def test_cookie_and_server_expiry_match_choice(tmp_path, remember):
    app=make_app(tmp_path); client=app.test_client()
    response=client.post('/api/auth/register',json={'email':'remember@example.test','nickname':'test','password':'synthetic-password','remember':remember})
    assert response.status_code==201
    assert ('Expires=' in response.headers['Set-Cookie']) is remember
    db=storage.connect(app.config['DATABASE'])
    remaining=db.execute('SELECT expires_at FROM sessions').fetchone()[0]-time.time()
    db.close()
    expected=30*86400 if remember else 12*3600
    assert expected-10 < remaining <= expected
    assert client.get('/api/me').json['user']

def test_login_survives_new_instance_and_local_secret_loss(tmp_path, monkeypatch):
    monkeypatch.delenv('COMEBACK_SECRET_KEY', raising=False)
    app=make_app(tmp_path); client=app.test_client()
    client.post('/api/auth/register',json={'email':'remember@example.test','nickname':'test','password':'synthetic-password','remember':True})
    cookie=client.get_cookie('session').value
    (tmp_path/'.session-secret').unlink()
    restarted=make_app(tmp_path)
    assert restarted.secret_key==app.secret_key
    returning=restarted.test_client(); returning.set_cookie('session',cookie)
    user=returning.get('/api/me').json
    assert user['user']['email']=='remember@example.test'
    returning.post('/api/auth/logout',json={},headers={'X-CSRF-Token':user['csrf_token']})
    returning.set_cookie('session',cookie)
    assert returning.get('/api/me').json['user'] is None
