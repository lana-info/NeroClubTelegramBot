"""Scoped bot API extension for the deployed PostgreSQL license service.

Installed beside license_service.http_server; no administrative browser session.
The bot service secret authenticates requests; the existing server vault key
encrypts recoverable keys so a lost HTTP reply never creates a second license.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from .config import ServiceConfig
from .licensing import _key_digest


PATH = "/internal/club-vip"


def provision(service, payload, vault_key):
    tid = payload.get("telegram_id")
    action = payload.get("action", "provision")
    known = payload.get("known", [])
    if type(tid) is not int or not 0 < tid < 2**52 or action not in {"provision", "suspend", "restore"}:
        raise ValueError("invalid request")
    if not isinstance(known, list) or len(known) > 100:
        raise ValueError("invalid known licenses")
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"nero-club-vip-v1\0" + vault_key).digest()))
    now = datetime.now(timezone.utc)
    result, unavailable = [], []
    with service._pool.connection() as db:
        with db.transaction():
            # Serializes initial creation, retries and suspension for this owner.
            db.execute("SELECT pg_advisory_xact_lock(%s)", (tid,))
            service._connection_state.connection = db
            try:
                if action != "provision":
                    grants = db.execute("SELECT * FROM club_vip_grants WHERE telegram_id=%s AND managed=true FOR UPDATE", (tid,)).fetchall()
                    for grant in grants:
                        if action == "suspend":
                            changed = db.execute("UPDATE licenses SET status='suspended' WHERE id=%s AND status='active' RETURNING id", (grant["license_id"],)).fetchone()
                            if changed:
                                db.execute("UPDATE club_vip_grants SET suspended_by_club=true WHERE license_id=%s", (grant["license_id"],))
                        elif grant["suspended_by_club"]:
                            db.execute("UPDATE licenses SET status='active' WHERE id=%s AND status='suspended'", (grant["license_id"],))
                            db.execute("UPDATE club_vip_grants SET suspended_by_club=false WHERE license_id=%s", (grant["license_id"],))
                    return {"licenses": [], "unavailable": []}
                catalog = db.execute("SELECT p.id,p.code,p.name,c.bundle_id FROM club_vip_products c JOIN products p ON p.id=c.product_id WHERE c.enabled AND p.status='active' ORDER BY p.code FOR SHARE OF c,p").fetchall()
                for product in catalog:
                    grant = db.execute("SELECT * FROM club_vip_grants WHERE telegram_id=%s AND product_id=%s FOR UPDATE", (tid,product["id"])).fetchone()
                    candidate = next((k for k in known if isinstance(k,dict) and k.get("product_id")==product["code"]), None)
                    if grant is None and candidate:
                        lid = str(uuid.UUID(candidate["license_id"]))
                        key = candidate["key"]
                        if not isinstance(key,str) or len(key)>256:
                            raise ValueError("invalid existing key")
                        record = db.execute("SELECT key_lookup_hmac FROM licenses WHERE id=%s AND product_id=%s", (lid,product["id"])).fetchone()
                        if not record or not hmac.compare_digest(record["key_lookup_hmac"],_key_digest(key,service._lookup_secret)):
                            raise ValueError("existing key mismatch")
                        db.execute("INSERT INTO club_vip_grants(telegram_id,product_id,license_id,encrypted_key,managed) VALUES (%s,%s,%s,%s,false)", (tid,product["id"],lid,cipher.encrypt(key.encode()).decode()))
                    elif grant is None:
                        # Never replace a known historical license whose code was lost.
                        contact = str(payload.get("username") or "").lstrip("@").lower()
                        email = str(payload.get("email") or "").lower()
                        historic = db.execute("SELECT id FROM licenses WHERE product_id=%s AND (order_id LIKE %s OR lower(customer_contact)=%s OR (%s<>'' AND lower(customer_contact)=%s) OR (%s<>'' AND lower(customer_contact) LIKE %s)) LIMIT 1", (product["id"],f"%{tid}%",str(tid),email,email,contact,f"%@{contact}%")).fetchone()
                        if historic:
                            unavailable.append(product["code"])
                            continue
                        key, record = service.generate_license(product["code"],now,license_term="perpetual",customer_contact=f"Telegram {tid}",order_id=f"club-vip:{tid}:{product['code']}",internal_comment="Automatic Club VIP grant; existing personal licenses unchanged")
                        lid = str(record.license_id)
                        db.execute("INSERT INTO club_vip_grants(telegram_id,product_id,license_id,encrypted_key,managed) VALUES (%s,%s,%s,%s,true)", (tid,product["id"],lid,cipher.encrypt(key.encode()).decode()))
                    grant = db.execute("SELECT * FROM club_vip_grants WHERE telegram_id=%s AND product_id=%s", (tid,product["id"])).fetchone()
                    record = db.execute("SELECT status,expires_at FROM licenses WHERE id=%s FOR UPDATE", (grant["license_id"],)).fetchone()
                    if grant["managed"] and grant["suspended_by_club"] and record["status"] == "suspended":
                        db.execute("UPDATE licenses SET status='active' WHERE id=%s", (grant["license_id"],))
                        db.execute("UPDATE club_vip_grants SET suspended_by_club=false WHERE license_id=%s", (grant["license_id"],))
                        record["status"] = "active"
                    if record["status"] != "active" or (record["expires_at"] and record["expires_at"] <= now) or not grant["encrypted_key"]:
                        unavailable.append(product["code"])
                        continue
                    if product["bundle_id"] and grant["managed"]:
                        assigned = db.execute("SELECT 1 FROM license_access_assignments WHERE license_id=%s AND source_kind='club_vip' AND source_ref=%s AND status='active'", (grant["license_id"],f"club-vip:{tid}")).fetchone()
                        if not assigned:
                            service.assign_bundle(str(grant["license_id"]),str(product["bundle_id"]),"club_vip",f"club-vip:{tid}",now,ends_at=None)
                    result.append({"license_id":str(grant["license_id"]),"product_id":product["code"],"app_name":product["name"],"key":cipher.decrypt(grant["encrypted_key"].encode()).decode(),"expires_at":record["expires_at"].isoformat() if record["expires_at"] else None,"managed":grant["managed"]})
            finally:
                service._connection_state.connection = None
    return {"licenses":result,"unavailable":unavailable}


def handle(handler):
    secret = os.getenv("CLUB_VIP_SERVICE_SECRET", "")
    if len(secret) < 32:
        handler._write_error(503,"CLUB_NOT_CONFIGURED")
        return
    try:
        length = int(handler.headers.get("Content-Length","-1"))
        if not 0 < length <= 65536:
            raise ValueError("invalid length")
        timestamp = handler.headers.get("X-Club-Timestamp", "")
        if abs(time.time()-int(timestamp)) > 60:
            raise PermissionError()
        raw = handler.rfile.read(length)
        signature = hmac.new(secret.encode(), timestamp.encode()+b"."+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,handler.headers.get("X-Club-Signature", "")):
            raise PermissionError()
        payload = json.loads(raw)
        if not isinstance(payload,dict):
            raise ValueError("object required")
        config = ServiceConfig.from_environment()
        if not config.admin_state_key:
            raise ValueError("vault unavailable")
        result = provision(handler.api._service,payload,config.admin_state_key)
        handler._write_json(200,result)
    except PermissionError:
        handler._write_error(401,"UNAUTHORIZED")
    except (ValueError,KeyError,TypeError):
        handler._write_error(409,"CLUB_REQUEST_REJECTED")
    except Exception:
        # Never reflect DB/crypto exception details or request bodies.
        handler._write_error(503,"CLUB_TEMPORARILY_UNAVAILABLE")
