"""帳號的線下管理工具。

存在的理由只有兩個：
  1. v1→v2 遷移建出來的帳號 password_hash 是 NULL，沒有任何 HTTP 端點能設定它
     （註冊端點會拒絕已存在的帳號）。要認領既有進度只能從這裡。
  2. 出事時要能從機器上直接看、直接撤 session、直接刪帳號，不必先登入。

刻意沒有 HTTP 版本：能建立管理員權限的端點，就是能被攻擊的端點。

    uv run python tools/useradmin.py list
    uv run python tools/useradmin.py set-password <username>
    uv run python tools/useradmin.py logout-all <username>
    uv run python tools/useradmin.py delete <username>
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db  # noqa: E402


def _user_or_exit(conn, username: str):
    user = auth.find_user(conn, username)
    if user is None:
        sys.exit(f"沒有這個帳號：{username}")
    return user


def cmd_list(_args) -> None:
    db.init()
    with db.session() as conn:
        rows = conn.execute(
            """SELECT u.id, u.username, u.password_hash IS NOT NULL AS can_login,
                      u.created_at, u.last_seen_at,
                      (SELECT COUNT(*) FROM session s WHERE s.user_id = u.id) sessions,
                      (SELECT COUNT(*) FROM card_state c WHERE c.user_id = u.id) cards,
                      (SELECT COUNT(*) FROM review_event e WHERE e.user_id = u.id) events
               FROM user u ORDER BY u.id"""
        ).fetchall()
    if not rows:
        print("（沒有任何帳號）")
        return
    print(f"{'id':>3}  {'username':<20} {'登入':<4} {'session':>7} {'卡':>6} {'事件':>6}  建立")
    for r in rows:
        print(
            f"{r['id']:>3}  {r['username']:<20} {'可' if r['can_login'] else '否':<4}"
            f" {r['sessions']:>7} {r['cards']:>6} {r['events']:>6}  {r['created_at'][:10]}"
        )


def cmd_set_password(args) -> None:
    db.init()
    password = getpass.getpass("新密碼：")
    if password != getpass.getpass("再輸入一次："):
        sys.exit("兩次輸入不一致")
    try:
        auth.validate_password(password)
    except Exception as exc:  # HTTPException 在 CLI 裡沒有意義，攤成訊息
        sys.exit(getattr(exc, "detail", str(exc)))

    code = auth.new_recovery_code()
    with db.session(write=True) as conn:
        user = _user_or_exit(conn, args.username)
        conn.execute(
            "UPDATE user SET password_hash=?, recovery_hash=? WHERE id=?",
            (auth.hash_password(password), auth.hash_recovery_code(code), user["id"]),
        )
        # 改密碼一律撤掉所有 session，理由同 /api/auth/password
        auth.drop_all_sessions(conn, user["id"])
    print(f"已設定 {user['username']} 的密碼，所有 session 已撤銷。")
    print(f"救援碼（只顯示這一次，抄下來）：{code}")


def cmd_logout_all(args) -> None:
    db.init()
    with db.session(write=True) as conn:
        user = _user_or_exit(conn, args.username)
        auth.drop_all_sessions(conn, user["id"])
    print(f"已撤銷 {user['username']} 的所有 session。")


def cmd_delete(args) -> None:
    db.init()
    with db.session(write=True) as conn:
        user = _user_or_exit(conn, args.username)
        cards = conn.execute(
            "SELECT COUNT(*) n FROM card_state WHERE user_id=?", (user["id"],)
        ).fetchone()["n"]
        if input(f"要刪除 {user['username']}（{cards} 張卡的進度）嗎？打 yes：") != "yes":
            sys.exit("取消")
        conn.execute("DELETE FROM user WHERE id=?", (user["id"],))
    print(f"已刪除 {user['username']}。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    for name, func in (
        ("set-password", cmd_set_password),
        ("logout-all", cmd_logout_all),
        ("delete", cmd_delete),
    ):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.set_defaults(func=func)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
