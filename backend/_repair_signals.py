"""One-off repair: re-sync signals.db with MT5's real state.

Earlier bugs settled trades against a stale position list and mock seed
prices, so open trades were recorded closed and real P/L was lost. This
rebuilds each ticketed signal from what MT5 actually reports.
"""
import sqlite3

import mt5_direct

BREAKEVEN_MONEY = 0.50

mt5_direct._load()
m = mt5_direct._mt5
live = {p.ticket for p in (m.positions_get() or [])}

conn = sqlite3.connect("signals.db")
conn.row_factory = sqlite3.Row
rows = list(conn.execute("SELECT id, ticket, symbol FROM signals WHERE ticket IS NOT NULL"))
info = mt5_direct.get_close_info([r["ticket"] for r in rows])

fixed = reopened = 0
for r in rows:
    t = r["ticket"]
    if t in live:
        conn.execute(
            "UPDATE signals SET status='open', closed_at=NULL, exit_price=NULL, profit=NULL WHERE id=?",
            (r["id"],),
        )
        reopened += 1
        print(f"  #{r['id']} {r['symbol']} ticket {t} -> still OPEN in MT5")
    elif t in info:
        d = info[t]
        net = d["net_profit"]
        status = "win" if net > BREAKEVEN_MONEY else ("loss" if net < -BREAKEVEN_MONEY else "breakeven")
        conn.execute(
            "UPDATE signals SET status=?, closed_at=?, exit_price=?, profit=? WHERE id=?",
            (status, d["closed_at"], d["exit_price"], net, r["id"]),
        )
        fixed += 1
        print(f"  #{r['id']} {r['symbol']} ticket {t} -> {status} (real P/L {net:+.2f})")

conn.commit()
conn.close()
print(f"\nrepaired {fixed} closed, {reopened} still-open")
