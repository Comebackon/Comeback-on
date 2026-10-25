"""Web push: private persistent VAPID key and restricted push-service endpoints."""
import base64
import json
from urllib.parse import urlsplit
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from pywebpush import webpush, WebPushException


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def keys(db):
    existing = db.execute('SELECT * FROM push_keys WHERE id=1').fetchone()
    if existing:
        return existing
    key = ec.generate_private_key(ec.SECP256R1())
    private = encoded(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = encoded(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    with db:
        db.execute("INSERT INTO push_keys(id,private_key,public_key) VALUES(1,?,?) ON CONFLICT(id) DO NOTHING", (private, public))
    return db.execute("SELECT * FROM push_keys WHERE id=1").fetchone()


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('잘못된 알림 구독 정보예요.')
    endpoint = value.get('endpoint', '')
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValueError('잘못된 알림 주소예요.')
    url = urlsplit(endpoint)
    host = url.hostname or ''
    allowed = host in {'fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com'} or host.endswith('.push.apple.com') or host.endswith('.notify.windows.com')
    if url.scheme != 'https' or not allowed or url.port not in (None, 443) or url.username or url.password or url.fragment:
        raise ValueError('지원하지 않는 알림 서버예요.')
    material = value.get('keys', {})
    for name, length in [('p256dh', 65), ('auth', 16)]:
        raw = material.get(name, '') if isinstance(material, dict) else ''
        try:
            decoded = base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True)
            if len(decoded) != length:
                raise ValueError()
            if name == 'p256dh':
                ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded)
        except Exception:
            raise ValueError('잘못된 알림 암호화 키예요.') from None
    return {'endpoint': endpoint, 'keys': {k: material[k] for k in ('p256dh', 'auth')}}


def send(db, user_id, private_key, notification_id=None):
    accepted = 0
    rows = db.execute('SELECT endpoint,subscription FROM push_subscriptions WHERE user_id=? LIMIT 5', (user_id,)).fetchall()
    for row in rows:
        try:
            webpush(subscription_info=validate(json.loads(row['subscription'])),
                    data=json.dumps({'title': '귀가 ON', 'body': '새 보호자 알림이 있어요. 사이트에서 확인해 주세요.', 'notification_id': notification_id}),
                    vapid_private_key=private_key, vapid_claims={'sub': 'https://comeback-on.kr'},
                    ttl=300, timeout=3)
            accepted += 1
        except WebPushException as error:
            if error.response is not None and error.response.status_code in (404, 410):
                with db:
                    db.execute('DELETE FROM push_subscriptions WHERE endpoint=?', (row['endpoint'],))
        except Exception:
            pass  # No endpoints, credentials or user content in logs; in-app record remains.
    return accepted
