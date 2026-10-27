import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from app.access import upsert_user
from app.db import Database
from app.integrations.club_licenses import ClubLicenseClient
from app.integrations.wordpress import WordPressClient
from app.keys import create_app_key
from app.membership import reconcile_members
from app.site_access import process_pending_site_access_jobs, queue_site_access_job
from app.telegram_updates import process_update


class Telegram:
    def __init__(self, member=None):
        self.member = member or {"status":"member"}
        self.messages = []

    async def get_chat_member(self, chat_id, user_id):
        assert str(chat_id) == "-100"
        return self.member

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id,text))


def database(tmp_path):
    db = Database(f"sqlite:///{tmp_path/'club.db'}")
    db.init_schema()
    return db


def update(text, ident=1):
    return {"update_id":ident,"message":{"chat":{"id":42,"type":"private"},"from":{"id":42},"text":text}}


@pytest.mark.parametrize("whitelist", [0, 1])
@pytest.mark.parametrize("member,override,banned,allowed", [
    ({"status":"member"},"none",0,True),
    ({"status":"administrator"},"none",0,True),
    ({"status":"creator"},"none",0,True),
    ({"status":"restricted","is_member":True},"none",0,True),
    ({"status":"restricted","is_member":False},"none",0,False),
    ({"status":"left"},"allow",0,False),
    ({"status":"kicked"},"allow",0,False),
    ({"status":"member"},"deny",0,False),
    ({"status":"member"},"none",1,False),
])
def test_unpaid_members_get_site_and_others_do_not(tmp_path,member,override,banned,allowed,whitelist):
    calls = []
    def wp(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={"user_id":17,"login":"nero_tg_42"})
    wordpress = WordPressClient("https://site.test","test",transport=httpx.MockTransport(wp))
    tg = Telegram(member)
    with database(tmp_path).connect() as db:
        upsert_user(db,{"telegram_id":42})
        db.execute("UPDATE users SET whitelist=?,access_override=?,telegram_banned=?",(whitelist,override,banned))
        for ident,text in enumerate(["/site_access@Etsy_NeuroClub_Bot","🌐 Доступ к сайту"],1):
            assert asyncio.run(process_update(db,update(text,ident),tg,wordpress=wordpress,club_chat_id="-100",technical_email=True)) == "processed"
        user = db.execute("SELECT * FROM users").fetchone()
        assert bool(user["site_credentials_delivered_at"]) is allowed
        if allowed:
            assert user["wordpress_user_id"] == 17
            assert user["wordpress_login"] == "nero_tg_42"
            assert user["wordpress_email"] == "tg.42@example.invalid"
            assert len([c for c in calls if c["action"] == "create_or_activate"]) == 1
            assert "Первоначальный пароль:" in tg.messages[0][1]
            assert "https://wiki.etsyneuroclub.com" in tg.messages[0][1]
            assert all(target == 42 for target,_ in tg.messages)
            assert calls[0]["password"] not in json.dumps(dict(user))
        else:
            assert calls == []


def test_channel_does_not_remove_group_entitlement_or_queue_deactivation(tmp_path):
    tg = Telegram({"status":"left"})
    with database(tmp_path).connect() as db:
        user = upsert_user(db,{"telegram_id":42})
        db.execute("UPDATE users SET telegram_membership_status='member'")
        event = {"update_id":11,"chat_member":{"chat":{"id":-200},"new_chat_member":{"status":"kicked","user":{"id":42}}}}
        assert asyncio.run(process_update(db,event,tg,chat_id=("-100","-200"),club_chat_id="-100")) == "ignored"
        asyncio.run(reconcile_members(db,tg,"-100",dry_run=True,removal_enabled=False,primary_group=False))
        assert db.execute("SELECT telegram_membership_status FROM users").fetchone()[0] == "member"
        assert db.execute("SELECT count(*) FROM outbox_jobs").fetchone()[0] == 0
        queue_site_access_job(db,user["id"],"deactivate","stale-channel-left")
        class NoWordPress:
            async def sync_user(self,*args):
                pytest.fail("A stale channel job must not deactivate an active group member")
        assert asyncio.run(process_pending_site_access_jobs(db,Telegram(),NoWordPress(),club_chat_id="-100"))["processed"] == 1


def test_key_request_imports_once_and_preserves_personal_key(tmp_path):
    enc = Fernet.generate_key().decode()
    payloads = []
    def server(request):
        body = json.loads(request.content)
        assert request.headers.get("X-Club-Signature")
        payloads.append(body)
        return httpx.Response(200,json={"licenses":[{"license_id":"00000000-0000-0000-0000-000000000001","product_id":"clipart-generator","app_name":"Clipart Generator","key":"old-key","managed":False,"expires_at":None},{"license_id":"00000000-0000-0000-0000-000000000002","product_id":"trendwatcher-video","app_name":"TrendWatcher Video","key":"new-key","managed":True,"expires_at":None}],"unavailable":[]})
    client = ClubLicenseClient("https://license.test","s"*32,transport=httpx.MockTransport(server))
    tg = Telegram()
    with database(tmp_path).connect() as db:
        user = upsert_user(db,{"telegram_id":42})
        old = create_app_key(db,{"key_id":"license-00000000-0000-0000-0000-000000000001","app_name":"Clipart Generator","key":"old-key","access_plan":"license","user_id":user["id"]},enc)
        old = dict(old)
        for i in (1,2):
            asyncio.run(process_update(db,update("/my_keys",i),tg,club_chat_id="-100",app_keys_encryption_key=enc,club_licenses=client))
        assert db.execute("SELECT count(*) FROM app_keys").fetchone()[0] == 2
        assert dict(db.execute("SELECT * FROM app_keys WHERE id=?",(old["id"],)).fetchone()) == old
        assert all("old-key" in text and "new-key" in text for _,text in tg.messages)
        assert len(payloads[0]["known"]) == 1 and len(payloads[1]["known"]) == 2
        # Club exit still permits the independent purchase, never the club key.
        tg.member = {"status":"left"}
        asyncio.run(process_update(db,update("/my_keys",3),tg,club_chat_id="-100",app_keys_encryption_key=enc,club_licenses=client))
        assert "old-key" in tg.messages[-1][1] and "new-key" not in tg.messages[-1][1]
        assert len(payloads) == 2
