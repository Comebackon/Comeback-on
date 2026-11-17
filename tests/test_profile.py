"""닉네임 수정과 프로필 사진. 사진은 정사각형으로 줄여 DB에 저장하고 연결된 사람에게만 보여준다."""
import io
import sqlite3

import pytest
from PIL import Image

from webapp import create_app


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "DATABASE": str(tmp_path / "profile.sqlite3"),
                       "SECRET_KEY": "test-only-secret"})


def user(app, name):
    client = app.test_client()
    response = client.post("/api/auth/register", json={"email": f"{name}@example.test", "password": "testing-12345", "nickname": name})
    assert response.status_code == 201, response.get_json()
    return client


def token(client):
    return client.get("/api/me").get_json()["csrf_token"]


def change(client, method, path, data=None):
    return client.open(path, method=method, json=data or {}, headers={"X-CSRF-Token": token(client)})


def picture(fmt="PNG", size=(400, 200), color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    out.seek(0)
    return out


def upload(client, data, name="me.png"):
    return client.post("/api/profile/photo", data={"photo": (data, name)}, headers={"X-CSRF-Token": token(client)}, content_type="multipart/form-data")


def connect(owner, guardian_name, guardian):
    gid = change(owner, "POST", "/api/guardians", {"email": f"{guardian_name}@example.test"}).get_json()["guardian"]["id"]
    return gid, change(guardian, "PATCH", f"/api/guardians/{gid}", {"action": "accept"})


def test_nickname_can_be_changed_within_ten_characters(app):
    client = user(app, "하늘")
    response = change(client, "PATCH", "/api/profile", {"nickname": "  새이름  "})
    assert response.status_code == 200
    assert response.get_json()["user"]["nickname"] == "새이름"
    assert client.get("/api/me").get_json()["user"]["nickname"] == "새이름"
    assert change(client, "PATCH", "/api/profile", {"nickname": "가나다라마바사아자차"}).status_code == 200
    for bad in ("가나다라마바사아자차카", "", "   ", None, 123):
        assert change(client, "PATCH", "/api/profile", {"nickname": bad}).status_code == 400


def test_nickname_change_shows_to_connected_people(app):
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    assert change(owner, "PATCH", "/api/profile", {"nickname": "바뀜"}).status_code == 200
    people = [g["person"]["nickname"] for g in guardian.get("/api/guardians").get_json()["guardians"]]
    assert people == ["바뀜"]


def test_profile_endpoints_need_login_and_csrf(app):
    guest = app.test_client()
    assert guest.patch("/api/profile", json={"nickname": "x"}).status_code in (401, 403)
    assert guest.post("/api/profile/photo", data={"photo": (picture(), "a.png")}, content_type="multipart/form-data").status_code in (401, 403)
    client = user(app, "하늘")
    assert client.patch("/api/profile", json={"nickname": "x"}).status_code == 403
    assert client.post("/api/profile/photo", data={"photo": (picture(), "a.png")}, content_type="multipart/form-data").status_code == 403


def test_photo_is_cropped_to_a_small_square_and_saved_in_the_database(app):
    client = user(app, "하늘")
    assert client.get("/api/me").get_json()["user"]["avatar_url"] is None
    response = upload(client, picture())
    assert response.status_code == 200, response.get_json()
    url = response.get_json()["user"]["avatar_url"]
    assert url and url.startswith("/api/avatar/")
    shown = client.get(url)
    assert shown.status_code == 200 and shown.mimetype == "image/jpeg"
    image = Image.open(io.BytesIO(shown.data))
    assert image.size == (256, 256) and image.format == "JPEG"
    assert sqlite3.connect(app.config["DATABASE"]).execute("SELECT count(*) FROM user_avatars").fetchone()[0] == 1


def test_photo_exif_and_original_metadata_are_dropped(app):
    client = user(app, "하늘")
    out = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "SECRET-CAMERA"
    Image.new("RGB", (300, 300), (0, 0, 200)).save(out, "JPEG", exif=exif)
    out.seek(0)
    assert upload(client, out, "IMG_1234.jpg").status_code == 200
    stored = client.get(client.get("/api/me").get_json()["user"]["avatar_url"]).data
    assert b"SECRET-CAMERA" not in stored and not Image.open(io.BytesIO(stored)).getexif()


def test_replacing_and_removing_the_photo(app):
    client = user(app, "하늘")
    first = upload(client, picture(color=(255, 0, 0))).get_json()["user"]["avatar_url"]
    second = upload(client, picture(color=(0, 255, 0))).get_json()["user"]["avatar_url"]
    assert sqlite3.connect(app.config["DATABASE"]).execute("SELECT count(*) FROM user_avatars").fetchone()[0] == 1
    assert client.get(second).status_code == 200
    removed = client.delete("/api/profile/photo", headers={"X-CSRF-Token": token(client)})
    assert removed.status_code == 200 and removed.get_json()["user"]["avatar_url"] is None
    assert client.get(second).status_code == 404
    assert first.split("?")[0] == second.split("?")[0]


@pytest.mark.parametrize("make", [
    lambda: (io.BytesIO(b"not an image"), "a.png"),
    lambda: (io.BytesIO(b"GIF89a" + b"\0" * 40), "a.gif"),
])
def test_invalid_files_are_rejected(app, make):
    client = user(app, "하늘")
    data, name = make()
    assert upload(client, data, name).status_code == 400
    assert client.get("/api/me").get_json()["user"]["avatar_url"] is None


def test_gif_and_oversized_photos_are_rejected(app):
    client = user(app, "하늘")
    assert upload(client, picture("GIF"), "a.gif").status_code == 400
    big = io.BytesIO(b"\xff\xd8" + b"0" * (3 * 1024 * 1024 + 10))
    assert upload(client, big, "big.jpg").status_code in (400, 413)
    assert client.post("/api/profile/photo", data={}, headers={"X-CSRF-Token": token(client)}, content_type="multipart/form-data").status_code == 400


def test_only_the_owner_and_accepted_connections_can_see_a_photo(app):
    owner, guardian, pending, stranger = (user(app, n) for n in ("owner", "guardian", "pending", "stranger"))
    url = upload(owner, picture()).get_json()["user"]["avatar_url"]
    uid = owner.get("/api/me").get_json()["user"]["id"]
    gid, accepted = connect(owner, "guardian", guardian)
    assert accepted.status_code == 200
    change(owner, "POST", "/api/guardians", {"email": "pending@example.test"})
    assert owner.get(url).status_code == 200
    assert guardian.get(url).status_code == 200
    assert pending.get(f"/api/avatar/{uid}").status_code == 404
    assert stranger.get(f"/api/avatar/{uid}").status_code == 404
    assert app.test_client().get(f"/api/avatar/{uid}").status_code == 401
    seen = guardian.get("/api/guardians").get_json()["guardians"][0]["person"]
    assert seen["avatar_url"] == url
    hidden = pending.get("/api/guardians").get_json()["guardians"][0]["person"]
    assert hidden["avatar_url"] is None
    assert change(guardian, "PATCH", f"/api/guardians/{gid}", {"action": "disconnect"}).status_code == 200
    assert guardian.get(url).status_code == 404


def test_deleting_the_account_removes_the_photo(app):
    client = user(app, "하늘")
    upload(client, picture())
    assert change(client, "DELETE", "/api/account", {"password": "testing-12345"}).status_code == 200
    assert sqlite3.connect(app.config["DATABASE"]).execute("SELECT count(*) FROM user_avatars").fetchone()[0] == 0
