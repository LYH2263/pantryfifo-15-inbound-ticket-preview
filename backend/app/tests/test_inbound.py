from datetime import date

from app.db import connect
from app.engines.inbound import make_ticket

SEED_LOTS = 5
SEED_DIRTY = 2
GOOD = [{"item_id": 1, "qty": 2, "expiry": "2099-01-01"}]


def lot_count():
    c = connect(); n = c.execute("SELECT COUNT(*) c FROM lots").fetchone()["c"]; c.close(); return n


def dirty_rows():
    c = connect()
    rows = [r["data_quality"] for r in c.execute("SELECT data_quality FROM lots WHERE data_quality='dirty'")]
    c.close(); return rows


def test_ticket_binds_content():
    t = make_ticket(GOOD)
    assert t == make_ticket([{"qty": 2.0, "expiry": "2099-01-01", "item_id": 1}])  # 键序/数量写法无关
    assert t != make_ticket([{"item_id": 1, "qty": 3, "expiry": "2099-01-01"}])
    assert t != make_ticket([{"item_id": 1, "qty": 2, "expiry": "2099-01-02"}])


def test_precheck_readonly(client):
    r = client.post("/api/inbound/precheck", json={"lines": GOOD})
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"].startswith("pf1_")
    line = body["lines"][0]
    assert line["name"] == "牛奶" and line["layer"] == "upper" and line["removable"] is False
    assert lot_count() == SEED_LOTS                          # 不落行
    assert len(client.get("/api/fridge").json()) == SEED_LOTS  # 全层批次数不变


def test_precheck_rejects_bad_lines(client):
    assert client.post("/api/inbound/precheck", json={"lines": [{"item_id": 1, "qty": 0, "expiry": "2099-01-01"}]}).status_code == 422
    assert client.post("/api/inbound/precheck", json={"lines": [{"item_id": 999, "qty": 1, "expiry": "2099-01-01"}]}).status_code == 422
    assert lot_count() == SEED_LOTS


def test_confirm_happy_layer_page_shows_same_batch(client):
    ticket = client.post("/api/inbound/precheck", json={"lines": GOOD}).json()["ticket"]
    r = client.post("/api/inbound/confirm", json={"ticket": ticket, "lines": GOOD})
    assert r.status_code == 200
    lot = r.json()["lots"][0]
    assert lot["layer"] == "upper" and lot["removable"] is False
    assert lot_count() == SEED_LOTS + 1
    layer_rows = client.get("/api/fridge?layer=upper").json()
    assert any(x["id"] == lot["id"] and x["qty_remain"] == 2 and x["expiry"] == "2099-01-01" for x in layer_rows)


def test_stale_ticket_rejected_no_write_no_cleanse(client):
    ticket = client.post("/api/inbound/precheck", json={"lines": GOOD}).json()["ticket"]
    changed_qty = [{"item_id": 1, "qty": 5, "expiry": "2099-01-01"}]
    r = client.post("/api/inbound/confirm", json={"ticket": ticket, "lines": changed_qty})
    assert r.status_code == 409 and r.json()["detail"] == "ticket_mismatch"
    changed_expiry = [{"item_id": 1, "qty": 2, "expiry": "2099-02-02"}]
    r2 = client.post("/api/inbound/confirm", json={"ticket": ticket, "lines": changed_expiry})
    assert r2.status_code == 409
    assert lot_count() == SEED_LOTS        # lots 不增行
    assert dirty_rows() == ["dirty"] * SEED_DIRTY  # 种子 dirty 行没被改成 clean


def test_confirm_bad_qty_or_unknown_item(client):
    for lines, reason in [
        ([{"item_id": 1, "qty": 0, "expiry": "2099-01-01"}], "qty_non_positive"),
        ([{"item_id": 1, "qty": -2, "expiry": "2099-01-01"}], "qty_non_positive"),
        ([{"item_id": 999, "qty": 1, "expiry": "2099-01-01"}], "item_not_found"),
    ]:
        r = client.post("/api/inbound/confirm", json={"ticket": make_ticket(lines), "lines": lines})
        assert r.status_code == 422 and r.json()["detail"] == reason
    assert lot_count() == SEED_LOTS        # 不增行
    assert dirty_rows() == ["dirty"] * SEED_DIRTY


def test_confirm_atomic_multi_line(client):
    lines = [{"item_id": 1, "qty": 1, "expiry": "2099-01-01"},
             {"item_id": 999, "qty": 1, "expiry": "2099-01-01"}]
    r = client.post("/api/inbound/confirm", json={"ticket": make_ticket(lines), "lines": lines})
    assert r.status_code == 422
    assert lot_count() == SEED_LOTS        # 整单拒写，无部分落行


def test_expired_batch_same_generation_across_views(client):
    yesterday = "2000-01-01"  # 确认时时钟已跨过票面到期日
    lines = [{"item_id": 2, "qty": 3, "expiry": yesterday}]
    pre = client.post("/api/inbound/precheck", json={"lines": lines}).json()
    assert pre["lines"][0]["removable"] is True
    r = client.post("/api/inbound/confirm", json={"ticket": pre["ticket"], "lines": lines})
    assert r.status_code == 200
    lot = r.json()["lots"][0]
    assert lot["removable"] is True
    # 同一代：全层看见 ⇔ 顶条报 ⇔ 下架名单有
    assert any(x["id"] == lot["id"] for x in client.get("/api/fridge?layer=mid").json())
    assert any(a["id"] == lot["id"] and a["level"] == "expired" for a in client.get("/api/alerts").json())
    assert any(x["id"] == lot["id"] for x in client.get("/api/removals").json())
    # 下架扫过后，三个视图同步不再见它
    assert lot["id"] in client.post("/api/expire-sweep").json()["expired_ids"]
    assert all(x["id"] != lot["id"] for x in client.get("/api/fridge?layer=mid").json())
    assert all(x["id"] != lot["id"] for x in client.get("/api/removals").json())
    assert all(a["id"] != lot["id"] for a in client.get("/api/alerts").json())


def test_today_expiry_not_yet_removable(client):
    today = date.today().isoformat()
    lines = [{"item_id": 1, "qty": 1, "expiry": today}]
    pre = client.post("/api/inbound/precheck", json={"lines": lines}).json()
    assert pre["lines"][0]["removable"] is False
    lot = client.post("/api/inbound/confirm", json={"ticket": pre["ticket"], "lines": lines}).json()["lots"][0]
    # 到期当日：顶条按临期报，但下架名单/下架扫还不动它
    assert any(a["id"] == lot["id"] and a["level"] == "soon" for a in client.get("/api/alerts").json())
    assert all(x["id"] != lot["id"] for x in client.get("/api/removals").json())
    assert lot["id"] not in client.post("/api/expire-sweep").json()["expired_ids"]
