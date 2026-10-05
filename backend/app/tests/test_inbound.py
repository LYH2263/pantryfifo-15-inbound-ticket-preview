from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


def lot_count(client):
    return len(client.get("/api/fridge").json())


def precheck(client, **kw):
    body = {"item_id": 1, "qty": 2, "expiry": (date.today() + timedelta(days=30)).isoformat()}
    body.update(kw)
    return client.post("/api/lots/precheck", json=body)


def confirm(client, p, **override):
    body = {"item_id": p["item_id"], "qty": p["qty"], "expiry": p["expiry"], "ticket": p["ticket"]}
    body.update(override)
    return client.post("/api/lots/confirm", json=body)


def test_precheck_returns_payload_and_writes_nothing(client):
    before = lot_count(client)
    r = precheck(client, item_id=1, qty=2)
    assert r.status_code == 200
    p = r.json()
    assert p["item_id"] == 1 and p["name"] == "牛奶"
    assert p["qty"] == 2 and p["expiry"] and p["layer"] == "upper"
    assert p["ticket"]
    assert p["total_lot_count"] == before  # 全层批次数不变
    assert lot_count(client) == before     # 预检不增行


def test_confirm_writes_and_layer_sees_same_lot(client):
    p = precheck(client, item_id=1, qty=2).json()
    r = confirm(client, p)
    assert r.status_code == 200
    lid = r.json()["id"]
    assert r.json()["layer"] == "upper"
    hits = [x for x in client.get("/api/fridge?layer=upper").json() if x["id"] == lid]
    assert hits and hits[0]["qty_remain"] == 2 and hits[0]["expiry"] == p["expiry"]


def test_confirm_with_stale_ticket_after_edit_fails(client):
    p = precheck(client, item_id=1, qty=2).json()
    before = lot_count(client)
    # 预检后改了数量或到期日，再拿旧票确认 → 必须失败
    assert confirm(client, p, qty=5).status_code == 409
    assert confirm(client, p, expiry=(date.today() + timedelta(days=60)).isoformat()).status_code == 409
    assert confirm(client, p, item_id=2).status_code == 409
    assert lot_count(client) == before  # lots 不增行


def test_failed_confirm_does_not_touch_dirty_rows(client):
    dirty_before = {x["id"]: x["data_quality"] for x in client.get("/api/fridge").json()
                    if x["data_quality"] == "dirty"}
    assert dirty_before  # 种子里有 dirty 行
    p = precheck(client, item_id=1, qty=2).json()
    assert confirm(client, p, qty=99).status_code == 409
    dirty_after = {x["id"]: x["data_quality"] for x in client.get("/api/fridge").json()
                   if x["data_quality"] == "dirty"}
    assert dirty_after == dirty_before  # 失败不得把 dirty 改成 clean


def test_confirm_non_positive_qty_fails(client):
    p = precheck(client, item_id=1, qty=2).json()
    before = lot_count(client)
    assert confirm(client, p, qty=0).status_code == 400
    assert confirm(client, p, qty=-3).status_code == 400
    assert lot_count(client) == before
    assert precheck(client, qty=0).status_code == 400


def test_confirm_unknown_item_fails(client):
    before = lot_count(client)
    assert precheck(client, item_id=999).status_code == 404
    r = client.post("/api/lots/confirm", json={
        "item_id": 999, "qty": 1,
        "expiry": (date.today() + timedelta(days=1)).isoformat(), "ticket": "x"})
    assert r.status_code == 404
    assert lot_count(client) == before


def test_confirm_after_clock_crossed_expiry_rejected(client, monkeypatch):
    # 预检时到期日尚在未来；确认时时钟已跨过票面到期日 → 整单拒写
    expiry = (date.today() + timedelta(days=1)).isoformat()
    p = precheck(client, item_id=1, expiry=expiry).json()
    before = lot_count(client)

    class LaterDate(date):
        @classmethod
        def today(cls):
            return super().today() + timedelta(days=2)

    monkeypatch.setattr("app.main.date", LaterDate)
    r = confirm(client, p)
    assert r.status_code == 409 and r.json()["detail"] == "expiry_passed"
    assert lot_count(client) == before  # 拒写：全层批次数不变


def test_precheck_rejects_already_passed_expiry(client):
    r = precheck(client, expiry=(date.today() - timedelta(days=1)).isoformat())
    assert r.status_code == 409 and r.json()["detail"] == "expiry_passed"


def test_confirmed_lot_consistent_across_views(client):
    # 同一世代：全层看见的新批，顶条同报，下架扫不动它（未到期）
    expiry = (date.today() + timedelta(days=1)).isoformat()  # warn_days=3 之内
    p = precheck(client, item_id=1, qty=2, expiry=expiry).json()
    lid = confirm(client, p).json()["id"]
    fridge_ids = {x["id"] for x in client.get("/api/fridge").json()}
    alert_ids = {x["id"] for x in client.get("/api/alerts").json()}
    swept = set(client.post("/api/expire-sweep").json()["expired_ids"])
    assert lid in fridge_ids
    assert lid in alert_ids
    assert lid not in swept
    assert lid in {x["id"] for x in client.get("/api/fridge").json()}  # 扫完仍在架
