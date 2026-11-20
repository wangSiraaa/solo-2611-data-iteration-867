"""可复跑的本地合成演示。

运行::

    python3 -m demo.run_demo          # 生成合成输入并打印结果
    python3 -m demo.run_demo --json   # 额外写出 demo/out/*.json

覆盖题面要求的案例：
1. 同车同挂精确抵消 (空车区间不扩大不确定性)
2. 仅换挂 (共享牵引车抵消、两挂车参与整数区间极值)
3. 目录迟到更正 (旧确认运输不变，新增带前序的重评)
4. 方向不可能 (拒绝并说明原因)
5. 同票竞争 (已确认运输占票，另一运输确认失败)
6. 重开 (对已确认运输重开，票仍归原运输)
外加：外部同格式快照、目录独立查询任意序号、每票货重与实体贡献说明。
"""

from __future__ import annotations

import argparse
import json
import pathlib

from weighbridge import (
    Catalog,
    IN,
    LOAD,
    OUT,
    TicketBusy,
    TicketRejected,
    UNLOAD,
    Warehouse,
)


def build_world() -> tuple[Catalog, Warehouse]:
    """合成目录与磅单，全部为本地整数记录。"""
    c = Catalog()
    # 稳定 ID 与空车质量整数区间 [lo, hi]，单位 kg
    c.define_mass("tractor", "T-101", 7050, 7150)
    c.define_mass("tractor", "T-202", 8200, 8250)
    c.define_mass("trailer", "R-A", 1550, 1650)
    c.define_mass("trailer", "R-B", 2100, 2250)

    # 序号动作：T-101 先挂 R-A，再换成 R-B；T-202 挂 R-A 空车
    c.apply_attach("T-101", "R-A")   # seq 1
    c.apply_detach("T-101", "R-A")   # seq 2
    c.apply_attach("T-101", "R-B")   # seq 3
    c.apply_attach("T-202", "R-A")   # seq 4

    w = Warehouse(c)
    # 全部 locall 合成：ts 为整数秒；同一车同一秒允许多张票
    w.add_ticket(dict(ticket_id="IN-001", tractor="T-101", catalog_seq=1,
                      ts=1000, direction=IN, gross=9000))
    w.add_ticket(dict(ticket_id="OUT-001", tractor="T-101", catalog_seq=1,
                      ts=1300, direction=OUT, gross=12800))
    w.add_ticket(dict(ticket_id="IN-002", tractor="T-101", catalog_seq=1,
                      ts=2000, direction=IN, gross=9100))
    w.add_ticket(dict(ticket_id="OUT-002", tractor="T-101", catalog_seq=3,
                      ts=2400, direction=OUT, gross=12500))
    # 竞争票：与 OUT-002 争同一张 IN-002
    w.add_ticket(dict(ticket_id="OUT-003", tractor="T-101", catalog_seq=3,
                      ts=2500, direction=OUT, gross=12900))
    # 方向不可能：入厂重、离厂轻，却声明 LOAD
    w.add_ticket(dict(ticket_id="IN-004", tractor="T-101", catalog_seq=3,
                      ts=3000, direction=IN, gross=12600))
    w.add_ticket(dict(ticket_id="OUT-004", tractor="T-101", catalog_seq=3,
                      ts=3400, direction=OUT, gross=9400))
    # 未挂挂车（detach 后）的 T-101 票，仍可引用 seq 2 的空载产品
    w.add_ticket(dict(ticket_id="IN-005", tractor="T-101", catalog_seq=2,
                      ts=4000, direction=IN, gross=8900))
    w.add_ticket(dict(ticket_id="OUT-005", tractor="T-101", catalog_seq=2,
                      ts=4300, direction=OUT, gross=8600))
    return c, w


def report(c: Catalog, w: Warehouse) -> dict:
    out: dict = {"catalog": {}, "trips": {}, "checks": []}

    # 目录独立查询任意序号处的组合身份 / 区间 / 来源
    for seq in range(c.last_seq() + 1):
        p = c.product_at(seq, "T-101")
        out["catalog"][f"T-101@{seq}"] = {
            "product_id": p.product_id,
            "identity": p.stable_identity(),
            "relation_src": p.relation_src,
            "masses": {m.kind: [m.lo, m.hi] for m in p.masses},
        }

    # 1) 同车同挂精确抵消
    ev1 = w.preview(LOAD, "IN-001", "OUT-001")
    w.confirm(ev1.trip_id)
    out["trips"]["exact_cancellation"] = {
        "trip_id": ev1.trip_id,
        "net": [ev1.net_lo, ev1.net_hi],
        "flow": [ev1.flow_lo, ev1.flow_hi],
        "cancellation": ev1.cancellation,
        "note": ev1.notes[-1],
    }

    # 2) 仅换挂：共享牵引车抵消，两挂车参与
    ev2 = w.preview(LOAD, "IN-002", "OUT-002")
    out["trips"]["swap_trailer"] = {
        "trip_id": ev2.trip_id,
        "net": [ev2.net_lo, ev2.net_hi],
        "flow": [ev2.flow_lo, ev2.flow_hi],
        "cancellation": ev2.cancellation,
        "contributions": [
            {"entity": x.label, "role": x.role,
             "coef": x.coefficient, "range": [x.lo, x.hi]}
            for x in ev2.contributions
        ],
        "ticket_in_goods": [ev2.derived_in.goods_lo, ev2.derived_in.goods_hi],
        "ticket_out_goods": [ev2.derived_out.goods_lo, ev2.derived_out.goods_hi],
    }
    w.confirm(ev2.trip_id)

    # 5) 同票竞争：OUT-003 与 OUT-002 争 IN-002，且 IN-002 已被确认
    ev3 = w.preview(LOAD, "IN-002", "OUT-003")
    try:
        w.confirm(ev3.trip_id)
        ev3_result = "unexpectedly confirmed"
    except TicketBusy as exc:
        ev3_result = f"rejected: {exc}"
    out["trips"]["ticket_competition"] = {
        "trip_id": ev3.trip_id,
        "status": w.trip(ev3.trip_id)["status"],
        "confirm": ev3_result,
        "ticket_usage": w.ticket_usage("IN-002"),
    }

    # 4) 方向不可能
    ev4 = w.preview(LOAD, "IN-004", "OUT-004")
    try:
        w.confirm(ev4.trip_id)
        ev4_result = "unexpectedly confirmed"
    except TicketRejected as exc:
        ev4_result = f"rejected: {exc}"
    out["trips"]["direction_impossible"] = {
        "trip_id": ev4.trip_id,
        "reason": ev4.reason,
        "confirm": ev4_result,
    }

    # 空载（detach 后）同组合精确抵消
    ev5 = w.preview(UNLOAD, "IN-005", "OUT-005")
    out["trips"]["empty_tractor_cancellation"] = {
        "trip_id": ev5.trip_id,
        "reason": ev5.reason,
        "net": None if ev5.net_lo is None else [ev5.net_lo, ev5.net_hi],
    }

    # 3) 目录迟到更正：修正 R-A 质量，重开已确认的换挂运输
    c.apply_set_mass("trailer", "R-A", 1500, 1600)   # seq 5
    re = w.reopen(ev2.trip_id)
    out["trips"]["late_correction_reopen"] = {
        "new_trip_id": re.trip_id,
        "predecessor_trip": re.predecessor_trip,
        "status": re.status,
        "net": [re.net_lo, re.net_hi],
        "in_product": re.derived_in.product.product_id,
        "out_product": re.derived_out.product.product_id,
        "original_trip_status": w.trip(ev2.trip_id)["status"],
        "original_net": [w.evaluation(ev2.trip_id).net_lo,
                         w.evaluation(ev2.trip_id).net_hi],
        "ticket_owner_still": w.ticket_usage("IN-002")["confirmed_trip"],
    }

    # 外部同格式快照：目录导出，独立仓库 (无 Catalog) 导入并消费
    snap = c.snapshot_at(3, "T-101")
    w_ext = Warehouse(None)
    w_ext.register_snapshot(snap.to_json())
    w_ext.add_ticket(dict(ticket_id="X-IN", tractor="T-101", catalog_seq=3,
                          ts=1, direction=IN, gross=9300))
    w_ext.add_ticket(dict(ticket_id="X-OUT", tractor="T-101", catalog_seq=3,
                          ts=2, direction=OUT, gross=9500))
    ext_ev = w_ext.preview(LOAD, "X-IN", "X-OUT")
    out["trips"]["external_snapshot"] = {
        "trip_id": ext_ev.trip_id,
        "product": ext_ev.derived_in.product.product_id,
        "net": [ext_ev.net_lo, ext_ev.net_hi],
        "reconcile": w_ext.reconcile(),
    }

    # 核对入口
    out["checks"] = {
        "warehouse_reconcile": w.reconcile(),
        "catalog_verify": c.verify(),
        "foreign_bindings": c.binding_history(),
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="写出 demo/out/*.json")
    args = ap.parse_args()

    c, w = build_world()
    data = report(c, w)
    print(json.dumps(data, indent=2, ensure_ascii=False))

    if args.json:
        out = pathlib.Path(__file__).parent / "out"
        out.mkdir(exist_ok=True)
        (out / "report.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # 同时导出目录轨迹与外部快照样例，便于手工核对
        (out / "catalog_actions.json").write_text(
            json.dumps([a.__dict__ for a in c.actions()], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (out / "snapshot_example.json").write_text(
            c.snapshot_at(3, "T-101").to_json(), encoding="utf-8"
        )
        print(f"\nwrote {out}/report.json", flush=True)


if __name__ == "__main__":
    main()
