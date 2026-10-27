from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import httpx

from ..keys import AppKeyError, create_app_key, decrypt_key


PRODUCT_NAMES = {
    "clipart generator": "clipart-generator", "clipart-generator": "clipart-generator",
    "trendwatcher video": "trendwatcher-video", "trendwatcher-video": "trendwatcher-video",
}


class ClubLicenseClient:
    def __init__(self, base_url, secret, *, transport=None):
        if not base_url or len(secret) < 32:
            raise ValueError("Club license integration is not configured")
        self.url = base_url.rstrip("/") + "/internal/club-vip"
        self.secret = secret
        self.transport = transport

    async def call(self, payload):
        raw = json.dumps(payload,ensure_ascii=False,separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(self.secret.encode(),timestamp.encode()+b"."+raw,hashlib.sha256).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=20,transport=self.transport) as client:
                response = await client.post(self.url,content=raw,headers={"Content-Type":"application/json","X-Club-Timestamp":timestamp,"X-Club-Signature":signature})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data,dict) or not isinstance(data.get("licenses"),list) or not isinstance(data.get("unavailable"),list):
                raise ValueError("invalid license response")
            return data
        except (httpx.HTTPError,ValueError) as exc:
            raise AppKeyError("club license service unavailable") from exc

    async def ensure_keys(self, db, user_id, encryption_key):
        user = db.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
        known = []
        for row in db.execute("SELECT * FROM app_keys WHERE assigned_user_id=? AND status='issued'",(user_id,)).fetchall():
            external = row["external_key_id"]
            product = PRODUCT_NAMES.get(row["app_name"].lower())
            mapping = db.execute("SELECT product_id FROM club_license_products WHERE external_key_id=?",(external,)).fetchone()
            product = mapping["product_id"] if mapping else product
            if product and external.startswith("license-"):
                known.append({"product_id":product,"license_id":external.removeprefix("license-"),"key":decrypt_key(row["encrypted_key"],encryption_key)})
        data = await self.call({"telegram_id":user["telegram_id"],"username":user["telegram_username"],"email":user["wordpress_email"],"known":known})
        validated = []
        try:
            for item in data["licenses"]:
                external = "license-" + str(uuid.UUID(item["license_id"]))
                if not isinstance(item["key"],str) or not item["key"] or type(item["managed"]) is not bool:
                    raise ValueError("invalid license")
                existing = db.execute("SELECT * FROM app_keys WHERE external_key_id=?",(external,)).fetchone()
                if existing and (existing["assigned_user_id"] != user_id or decrypt_key(existing["encrypted_key"],encryption_key) != item["key"]):
                    raise ValueError("existing license conflict")
                validated.append((external,item,existing))
        except (ValueError,KeyError,TypeError) as exc:
            raise AppKeyError("invalid license response") from exc
        for external,item,existing in validated:
            if not existing:
                create_app_key(db,{"key_id":external,"app_name":item["app_name"],"access_plan":"club" if item["managed"] else "license","key":item["key"],"key_expires_at":item["expires_at"],"user_id":user_id},encryption_key)
            db.execute("INSERT INTO club_license_products(external_key_id,product_id,managed) VALUES (?,?,?) ON CONFLICT(external_key_id) DO NOTHING",(external,item["product_id"],int(item["managed"])))
        withheld = set()
        for row in db.execute("SELECT k.app_name,m.product_id FROM app_keys k LEFT JOIN club_license_products m ON m.external_key_id=k.external_key_id WHERE k.assigned_user_id=?",(user_id,)).fetchall():
            product = row["product_id"] or PRODUCT_NAMES.get(row["app_name"].lower())
            if product in data["unavailable"]:
                withheld.add(row["app_name"])
        return {"unavailable":data["unavailable"],"withheld":withheld}


async def process_club_jobs(db, client, telegram, chat_id, *, dry_run=False):
    from ..club_access import refresh_club_member, ClubCheckError
    processed = failed = 0
    if dry_run:
        return {"processed":0,"failed":0}
    for job in db.execute("SELECT * FROM outbox_jobs WHERE kind='club.access' AND status='pending' ORDER BY id LIMIT 20").fetchall():
        try:
            user_id = json.loads(job["payload"])["user_id"]
            allowed = await refresh_club_member(db,user_id,telegram,chat_id)
            user = db.execute("SELECT telegram_id FROM users WHERE id=?",(user_id,)).fetchone()
            await client.call({"telegram_id":user["telegram_id"],"action":"restore" if allowed else "suspend"})
            db.execute("UPDATE outbox_jobs SET status='done',processed_at=CURRENT_TIMESTAMP WHERE id=?",(job["id"],))
            processed += 1
        except (AppKeyError,ClubCheckError,ValueError,KeyError):
            db.execute("UPDATE outbox_jobs SET attempts=attempts+1,last_error='club access sync failed' WHERE id=?",(job["id"],))
            failed += 1
    return {"processed":processed,"failed":failed}
