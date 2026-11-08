import json
import time
from types import SimpleNamespace as NS

import pytest

from conftest import VIDEO

OWNER = {"Authorization": "Bearer test-owner-token"}


@pytest.fixture(autouse=True)
def entitled(request, appmod, monkeypatch):
    """Most tests here are about the feature itself, so grant it; the gate
    tests opt out with the `locked` marker."""
    if "locked" not in request.keywords:
        monkeypatch.setattr(appmod, "get_entitlements", lambda req: {"plan": "free", "hardware": True})


def upload(client, **data):
    return client.post("/process", files={"file": ("t.mp4", VIDEO, "video/mp4")}, data=data)


def pair(client, name="PC"):
    r = client.post("/devices", json={"name": name})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def result_payload(n=1):
    manifest = [{"file": f"peakcut_rank{i}_v80_vertical.mp4", "srt": f"peakcut_rank{i}_v80.srt", "start": 1, "end": 20,
                 "virality_score": 80, "text": "hello there.", "title": "hello", "hashtags": ["#a"]} for i in range(1, n + 1)]
    files = []
    for c in manifest:
        files += [("files", (c["file"], b"video")), ("files", (c["srt"], b"1\n"))]
    return {"manifest": json.dumps(manifest), "duration": "60", "language": "en"}, files


def test_own_job_waits_for_paired_device_and_skips_quota(client):
    dev = pair(client)
    r = upload(client, device="own", n_clips="8")
    assert r.status_code == 200
    jid = r.json()["job_id"]
    st = client.get(f"/job/{jid}").json()
    assert st["status"] == "queued" and st["device"] == "own" and st["device_online"] is False
    assert client.get("/usage").json()["used"] == 0  # doesn't touch the monthly allowance

    spec = client.post("/worker/claim", headers=dev, json={"hostname": "pc"}).json()
    assert spec["job_id"] == jid and spec["options"]["n_clips"] == 8 and spec["watermark"] is True
    assert client.get(spec["input_url"], headers=dev).content == VIDEO
    assert client.post(f"/worker/jobs/{jid}/progress", headers=dev, json={"stage": "rendering", "progress": 60}).status_code == 200
    data, files = result_payload(2)
    assert client.post(f"/worker/jobs/{jid}/result", headers=dev, data=data, files=files).status_code == 200
    st = client.get(f"/job/{jid}").json()
    assert st["status"] == "done" and len(st["clips"]) == 2
    assert client.get(st["clips"][0]["url"]).content == b"video"
    assert client.get("/devices").json()["devices"][0]["online"] is True


def test_devices_are_isolated_between_users(client, appmod):
    from fastapi.testclient import TestClient
    other = TestClient(appmod.app)
    other.get("/usage")
    other_dev = pair(other)
    jid = upload(client, device="own").json()["job_id"]
    assert client.post("/worker/claim", headers=other_dev, json={}).status_code == 204
    # and the owner token never takes someone's own-hardware job
    assert client.post("/worker/claim", headers=OWNER, json={}).status_code == 204
    assert appmod._get_job(jid)["status"] == "queued"


def test_owner_worker_drains_cloud_queue(client, appmod):
    jid = upload(client).json()["job_id"]
    assert client.get("/usage").json()["used"] == 1
    spec = client.post("/worker/claim", headers=OWNER, json={}).json()
    assert spec["job_id"] == jid
    assert client.post("/worker/claim", headers=OWNER, json={}).status_code == 204  # no double claim
    client.post(f"/worker/jobs/{jid}/fail", headers=OWNER, json={"error": "boom"})
    st = client.get(f"/job/{jid}").json()
    assert st["status"] == "failed" and "didn't count" in st["error"]
    assert client.get("/usage").json()["used"] == 0  # refunded


def test_lease_expiry_requeues_then_gives_up(client, appmod):
    jid = upload(client).json()["job_id"]
    for attempt in range(3):
        assert client.post("/worker/claim", headers=OWNER, json={}).json()["job_id"] == jid
        appmod._set_job(jid, lease_until=time.time() - 1)
        appmod._expire_leases()
    assert appmod._get_job(jid)["status"] == "failed"


def test_worker_results_are_validated(client, appmod):
    dev = pair(client)
    jid = upload(client, device="own").json()["job_id"]
    client.post("/worker/claim", headers=dev, json={})
    bad = client.post(f"/worker/jobs/{jid}/result", headers=dev, data={"manifest": json.dumps([{"file": "../../app.py"}])},
                      files=[("files", ("../../app.py", b"x"))])
    assert bad.status_code == 400
    assert client.post(f"/worker/jobs/{jid}/result", headers=dev, data={"manifest": "nope"},
                       files=[("files", ("a.mp4", b"x"))]).status_code == 400
    data, files = result_payload(1)
    m = json.loads(data["manifest"]); m[0]["virality_score"] = 10_000; data["manifest"] = json.dumps(m)
    assert client.post(f"/worker/jobs/{jid}/result", headers=dev, data=data, files=files).status_code == 200
    assert client.get(f"/job/{jid}").json()["clips"][0]["virality_score"] == 100
    # finished job can't be overwritten
    assert client.post(f"/worker/jobs/{jid}/result", headers=dev, data=data, files=files).status_code == 409


def test_bad_tokens_and_device_limits(client):
    assert client.post("/worker/claim", headers={"Authorization": "Bearer pd_nope"}, json={}).status_code == 401
    assert client.post("/worker/claim", json={}).status_code == 401
    for _ in range(3):
        pair(client)
    assert client.post("/devices", json={}).status_code == 400
    dev_id = client.get("/devices").json()["devices"][0]["id"]
    assert client.delete(f"/devices/{dev_id}").json() == {"removed": True}
    assert len(client.get("/devices").json()["devices"]) == 2


def test_own_hardware_daily_cap(client, appmod, monkeypatch):
    monkeypatch.setattr(appmod.config, "OWN_DEVICE_DAILY_LIMIT", 2)
    monkeypatch.setattr(appmod, "MAX_OWN_QUEUED_PER_USER", 10)
    assert upload(client, device="own").status_code == 200
    assert upload(client, device="own").status_code == 200
    assert upload(client, device="own").status_code == 429


def test_worker_bundle_contains_what_the_worker_imports(client):
    import io
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(client.get("/worker/bundle.zip").content)).namelist()
    assert {"peakcut-worker/peakcut_worker.py", "peakcut-worker/pipeline_lib.py",
            "peakcut-worker/requirements-worker.txt"} <= set(names)


@pytest.mark.locked
def test_free_users_are_gated(client):
    assert client.post("/devices", json={"name": "x"}).status_code == 402
    r = upload(client, device="own")
    assert r.status_code == 402 and "Pro" in r.json()["detail"]
    u = client.get("/usage").json()
    assert u["own_hardware"] is False and u["used"] == 0
    assert upload(client).status_code == 200  # cloud still works


@pytest.mark.locked
def test_entitlements_from_stripe(appmod, monkeypatch):
    monkeypatch.setattr(appmod.config, "STRIPE_CONFIGURED", True)
    monkeypatch.setattr(appmod.config, "STRIPE_PRICE_ID_HARDWARE", "price_hw")
    monkeypatch.setattr(appmod.config, "STRIPE_PRICE_ID_PLUS", "price_plus")
    subs = {"cus_hw": ["price_hw"], "cus_pro": ["price_pro"], "cus_both": ["price_hw", "price_plus"], "cus_none": []}

    def fake_list(customer, status, limit):
        return NS(data=[{"status": "active", "items": {"data": [{"price": {"id": p}} for p in subs[customer]]}}])

    monkeypatch.setattr(appmod.stripe.Subscription, "list", fake_list)
    appmod.invalidate_plan_cache()
    assert appmod.entitlements_for_customer("cus_hw") == {"plan": "free", "hardware": True}
    assert appmod.entitlements_for_customer("cus_pro") == {"plan": "pro", "hardware": True}
    assert appmod.entitlements_for_customer("cus_both") == {"plan": "plus", "hardware": True}
    assert appmod.entitlements_for_customer("cus_none") == {"plan": "free", "hardware": False}
    # the add-on alone must not count as a paid plan anywhere else
    assert appmod.plan_for_customer("cus_hw") == "free"
