"""Run inside the license image with a disposable loopback PostgreSQL only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.request
import urllib.error

import psycopg
from psycopg.rows import dict_row
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from license_service.api import LicenseApi
from license_service.club_vip import provision
from license_service.http_server import make_server
from license_service.postgres_store import PostgresLicenseService


DSN = "host=127.0.0.1 port=5432 dbname=club_test user=postgres"


def main():
    # No configuration/env DSN is accepted, nor a published production socket.
    assert os.getenv("NERO_DISPOSABLE_CLUB_TEST") == "yes"
    vault,lookup = secrets.token_bytes(32),secrets.token_bytes(32)
    os.environ["LICENSE_SERVICE_ADMIN_STATE_KEY_B64"] = base64.urlsafe_b64encode(vault).decode()
    os.environ["CLUB_VIP_SERVICE_SECRET"] = "test-service-secret-" + secrets.token_hex(24)
    with psycopg.connect(DSN) as db:
        for migration in sorted(Path("/app/migrations").glob("*.sql")):
            db.execute(migration.read_text())
    service = PostgresLicenseService(lookup,DSN)
    server = None
    try:
        existing_products = {p["code"] for p in service.list_products()}
        for code,name in [("clipart-generator","Clipart Generator"),("trendwatcher-video","TrendWatcher Video")]:
            if code not in existing_products:
                service.create_product(code,name)
        bundle = service.create_bundle("clipart-generator","club-vip","Club VIP","managed",["patterns","studio"])
        with psycopg.connect(DSN) as db:
            db.execute(Path("/workspace/server_extension/club_vip.sql").read_text())
        api = LicenseApi(service,Ed25519PrivateKey.generate())
        server = make_server(api,port=0)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/internal/club-vip"
        def call(payload,signed=True):
            raw = json.dumps(payload,separators=(",", ":")).encode()
            stamp = str(int(time.time()))
            sig = hmac.new(os.environ["CLUB_VIP_SERVICE_SECRET"].encode(),stamp.encode()+b"."+raw,hashlib.sha256).hexdigest() if signed else "bad"
            req = urllib.request.Request(url,data=raw,headers={"Content-Type":"application/json","X-Club-Timestamp":stamp,"X-Club-Signature":sig})
            with urllib.request.urlopen(req,timeout=20) as response:
                return json.load(response)
        try:
            call({"telegram_id":42},False)
            raise AssertionError("unsigned request accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        payload = {"telegram_id":42}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:call(payload),range(4)))
        assert all(r == results[0] for r in results)
        first = results[0]
        assert len(first["licenses"]) == 2
        assert call(payload) == first  # persisted replay after lost response
        with psycopg.connect(DSN,row_factory=dict_row) as db:
            assert db.execute("SELECT count(*) n FROM licenses").fetchone()["n"] == 2
            assert db.execute("SELECT count(*) n FROM license_access_assignment_modules").fetchone()["n"] == 2
            assert db.execute("SELECT count(*) n FROM device_activations").fetchone()["n"] == 0
            grant = db.execute("SELECT encrypted_key FROM club_vip_grants LIMIT 1").fetchone()
            assert all(k["key"] not in grant["encrypted_key"] for k in first["licenses"])
        call({"telegram_id":42,"action":"suspend"})
        with psycopg.connect(DSN) as db:
            assert db.execute("SELECT count(*) FROM licenses WHERE status='suspended'").fetchone()[0] == 2
        call({"telegram_id":42,"action":"restore"})
        assert call(payload) == first
        old_key,old = service.generate_license("clipart-generator",datetime.now(timezone.utc),customer_contact="Telegram 77")
        adopted = call({"telegram_id":77,"known":[{"product_id":"clipart-generator","license_id":old.license_id,"key":old_key}]})
        old_result = next(k for k in adopted["licenses"] if k["product_id"]=="clipart-generator")
        assert old_result["key"] == old_key and old_result["managed"] is False
        call({"telegram_id":77,"action":"suspend"})
        with psycopg.connect(DSN) as db:
            assert db.execute("SELECT status FROM licenses WHERE id=%s",(old.license_id,)).fetchone()[0] == "active"
            # Manual revocation must never be undone by club restore.
            lid = first["licenses"][0]["license_id"]
            db.execute("UPDATE licenses SET status='revoked' WHERE id=%s",(lid,))
        call({"telegram_id":42,"action":"restore"})
        revoked = call(payload)
        assert len(revoked["licenses"]) == 1 and len(revoked["unavailable"]) == 1
        # Future catalog entries are issued on the next request.
        service.create_product("future-product","Future Product")
        with psycopg.connect(DSN) as db:
            db.execute("INSERT INTO club_vip_products(product_id) SELECT id FROM products WHERE code='future-product'")
        assert len(call(payload)["licenses"]) == 2
        # An old license without recoverable code blocks duplicate creation.
        service.generate_license("clipart-generator",datetime.now(timezone.utc),customer_contact="@lost_codes")
        missing = call({"telegram_id":88,"username":"lost_codes"})
        assert "clipart-generator" in missing["unavailable"]
        # Wrong proof is rejected and leaves no partial grants.
        try:
            call({"telegram_id":99,"known":[{"product_id":"clipart-generator","license_id":old.license_id,"key":"wrong"}]})
            raise AssertionError("wrong existing key accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 409
        with psycopg.connect(DSN) as db:
            assert db.execute("SELECT count(*) FROM club_vip_grants WHERE telegram_id=99").fetchone()[0] == 0
        print(json.dumps({"passed":True,"checks":["signed HTTP","concurrency","lost response retry","encrypted persistence","modules","no device slots consumed","suspend/restore","personal license preserved","revoked license preserved","future products","lost historical code","atomic rejection"]}))
    finally:
        if server:
            server.shutdown()
            server.server_close()
        service.close()


if __name__ == "__main__":
    main()
