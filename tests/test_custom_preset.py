"""custom preset：使用者自己調的答對／答錯倍率。

這個功能的規格只有一句話 ——「改了立刻生效，只對往後有用，不動之前的」——
但那句話在一個「事件日誌 + 整段重算」的系統裡不是免費的：重算會把每一筆事件
重新套一次，若它套的是「現在」這組倍率，那麼調一次倍率就等於把整段歷史改寫。
所以每筆事件都存著它當時那組數值（review_event.preset_params），
test_changing_multipliers_does_not_rewrite_history 就是在釘這件事。
"""

import json

import pytest

from app import db, srs


def _answer(client, deck="car", correct=True):
    cid = client.get("/api/queue/unlearned", params={"deck": deck}).json()["card_ids"][0]
    card = client.get(f"/api/card/{cid}").json()
    right = card["question"]["answer"]
    chosen = right if correct else next(x for x in (1, 2, 3) if x != right)
    return cid, client.post("/api/answer", json={"card_id": cid, "chosen": chosen}).json()


def test_switching_to_custom_seeds_from_the_current_preset(client):
    """切過去的第一眼要跟切之前一模一樣 —— custom 是「從這裡開始改」，不是重設。"""
    client.put("/api/settings", json={"preset": "intensive"})
    s = client.put("/api/settings", json={"preset": "custom"}).json()

    assert s["preset"] == "custom"
    assert s["params"]["remembered_mult"] == 1.4   # intensive 的值原樣帶過來
    assert s["params"]["forgot_mult"] == 0.35
    assert s["params"]["initial_hours"] == 8.0
    assert s["custom_params"]["base"] == "intensive"


def test_custom_multipliers_take_effect_on_the_next_answer(client):
    client.put("/api/settings", json={"preset": "custom"})
    client.put("/api/settings", json={
        "custom_params": {"base": "relaxed", "remembered_mult": 2.0, "forgot_mult": 0.25}
    })

    _, r = _answer(client, correct=True)
    assert r["interval_hours"] == 48.0     # 24（relaxed 的首次）× 2.0
    _, r = _answer(client, correct=False)
    assert r["interval_hours"] == 6.0      # 24 × 0.25 = 6，正好在下限上


def test_out_of_range_multipliers_are_clamped_not_rejected(client):
    """夾擠而不是 400：使用者打了 ×9 應該看到它被收回 5，而不是一個紅字錯誤。

    邊界本身是「排程還成不成立」的界線 —— 答對倍率 ≤ 1 的話間隔永遠不成長。
    """
    client.put("/api/settings", json={"preset": "custom"})
    s = client.put("/api/settings", json={
        "custom_params": {"remembered_mult": 99, "forgot_mult": 0.0001}
    }).json()
    assert s["params"]["remembered_mult"] == srs.REMEMBERED_MULT_RANGE[1]
    assert s["params"]["forgot_mult"] == srs.FORGOT_MULT_RANGE[0]

    s = client.put("/api/settings", json={
        "custom_params": {"remembered_mult": 0.5, "forgot_mult": 5}
    }).json()
    assert s["params"]["remembered_mult"] == srs.REMEMBERED_MULT_RANGE[0]
    assert s["params"]["forgot_mult"] == srs.FORGOT_MULT_RANGE[1]


def test_unknown_preset_is_still_rejected(client):
    assert client.put("/api/settings", json={"preset": "aggressive"}).status_code == 400


def test_changing_multipliers_does_not_rewrite_history(client):
    """這個功能的核心保證：調倍率只影響之後的作答。

    做法是「答一題 → 改倍率 → 觸發整段重算 → 那一題的間隔必須沒變」。重算走的是
    /api/answer/regrade（它會 DELETE 最後一筆再重打），但那會動到這張卡本身，
    所以改用兩張卡：A 答完後改倍率，再對 B 亂序補送逼出 A 的重算路徑並不成立 ——
    直接呼叫 db.rebuild_card 才是精確地測到那條路。
    """
    client.put("/api/settings", json={"preset": "custom"})
    client.put("/api/settings", json={
        "custom_params": {"base": "relaxed", "remembered_mult": 2.0, "forgot_mult": 0.5}
    })
    cid, r = _answer(client, correct=True)
    assert r["interval_hours"] == 48.0

    # 換一組完全不同的倍率
    client.put("/api/settings", json={
        "custom_params": {"base": "relaxed", "remembered_mult": 1.1, "forgot_mult": 0.9}
    })

    with db.session(write=True) as conn:
        cfg = db.user_settings(conn, 1)
        assert cfg.remembered_mult == 1.1            # 新設定確實生效了
        state = db.rebuild_card(conn, 1, cid, "car", cfg)

    # 舊事件用的是它當時那組（×2.0），重算後仍然是 48 小時 —— 沒有被改寫
    assert state.review_interval_hours == 48.0

    # 而下一題（新事件）用的是新倍率
    _, r2 = _answer(client, correct=True)
    assert r2["interval_hours"] == pytest.approx(24.0 * 1.1)


def test_event_carries_the_values_in_force_at_the_time(client):
    """事件裡存的是數值快照，不只是名字 —— 名字對 custom 而言不足以還原當時。"""
    client.put("/api/settings", json={"preset": "custom"})
    client.put("/api/settings", json={
        "custom_params": {"base": "intensive", "remembered_mult": 1.7, "forgot_mult": 0.3}
    })
    cid, _ = _answer(client, correct=True)

    with db.session() as conn:
        row = conn.execute(
            "SELECT preset, preset_params FROM review_event WHERE user_id=1 AND card_id=?",
            (cid,),
        ).fetchone()
    assert row["preset"] == "custom"
    saved = json.loads(row["preset_params"])
    assert saved["remembered_mult"] == 1.7 and saved["forgot_mult"] == 0.3
    assert saved["initial_hours"] == 8.0          # base=intensive 的首次間隔一起存著

    # 名字查不到數值時的還原路徑：resolve() 吃這份快照就能重現當時的參數
    assert srs.resolve("custom", saved).remembered_mult == 1.7


def test_named_presets_ignore_any_stored_custom_values(client):
    """切回具名 preset 之後，custom 的數值還留著（切回去就恢復），但不得生效。"""
    client.put("/api/settings", json={"preset": "custom"})
    client.put("/api/settings", json={
        "custom_params": {"base": "relaxed", "remembered_mult": 3.0, "forgot_mult": 0.2}
    })
    s = client.put("/api/settings", json={"preset": "intensive"}).json()
    assert s["params"]["remembered_mult"] == 1.4                  # 用 intensive 的
    assert s["custom_params"]["remembered_mult"] == 3.0           # 但沒被丟掉

    s = client.put("/api/settings", json={"preset": "custom"}).json()
    assert s["params"]["remembered_mult"] == 3.0                  # 切回去就恢復
