"""端到端测试：目录 / 仓库 / 求解 / 重评 / 核对。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import itertools
import json
import unittest
from fractions import Fraction

from weighbridge import (
    Catalog,
    CatalogError,
    IN,
    LOAD,
    OUT,
    Snapshot,
    STATUS_CONFIRMED,
    STATUS_DRAFT,
    STATUS_REVIEW,
    STATUS_SUPERSEDED,
    TicketBusy,
    TicketRejected,
    UNLOAD,
    Warehouse,
    WarehouseError,
)
from weighbridge.warehouse import (
    REASON_DIRECTION_IMPOSSIBLE,
    REASON_MASS_INTERVAL_CONFLICT,
    REASON_OK,
    REASON_TICKET_INFEASIBLE,
    REASON_TIME_ORDER,
    _solve_int,
)


def build_catalog() -> Catalog:
    c = Catalog()
    c.define_mass("tractor", "T1", 7000, 7200)
    c.define_mass("tractor", "T2", 8000, 8100)
    c.define_mass("trailer", "R1", 1500, 1600)
    c.define_mass("trailer", "R2", 2000, 2200)
    c.define_mass("trailer", "R3", 3000, 3050)
    return c


def ticket(tid, tractor, seq, ts, direction, gross):
    return {
        "ticket_id": tid, "tractor": tractor, "catalog_seq": seq,
        "ts": ts, "direction": direction, "gross": gross,
    }


class CatalogTests(unittest.TestCase):
    def test_initial_product_at_seq_zero(self):
        c = build_catalog()
        p = c.product_at(0, "T1")
        self.assertEqual(p.stable_identity(), ("T1", None))
        self.assertEqual(p.relation_src, "initial")
        self.assertEqual([(m.kind, m.lo, m.hi) for m in p.masses],
                         [("tractor", 7000, 7200)])

    def test_history_never_overwritten_by_later_swap(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        at1 = c.product_at(1, "T1")
        self.assertEqual(at1.stable_identity(), ("T1", "R1"))
        self.assertEqual(at1.relation_src, "attach")
        at2 = c.product_at(2, "T1")
        self.assertEqual(at2.stable_identity(), ("T1", None))
        self.assertEqual(at2.relation_src, "detach")
        at3 = c.product_at(3, "T1")
        self.assertEqual(at3.stable_identity(), ("T1", "R2"))
        # 历史产品不可变
        self.assertEqual(c.product(at1.product_id).stable_identity(), ("T1", "R1"))
        self.assertEqual(c.verify(), [])

    def test_sequence_must_strictly_increase(self):
        c = build_catalog()
        c.apply_attach("T1", "R1", seq=5)
        with self.assertRaises(CatalogError):
            c.apply_attach("T1", "R1", seq=5)
        with self.assertRaises(CatalogError):
            c.apply_detach("T1", "R1", seq=4)
        self.assertEqual(c.last_seq(), 5)

    def test_one_trailer_at_most_one_tractor(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        with self.assertRaises(CatalogError):
            c.apply_attach("T2", "R1")
        self.assertEqual(c.last_seq(), 1)  # 失败不占序号

    def test_one_tractor_at_most_one_trailer(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        with self.assertRaises(CatalogError):
            c.apply_attach("T1", "R2")
        self.assertEqual(c.last_seq(), 1)

    def test_detach_requires_matching_tractor(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        with self.assertRaises(CatalogError):
            c.apply_detach("T2", "R1")
        self.assertEqual(c.last_seq(), 1)

    def test_batch_is_atomic(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        with self.assertRaises(CatalogError):
            c.apply_batch([
                {"kind": "detach", "tractor": "T1", "trailer": "R1"},
                {"kind": "attach", "tractor": "T2", "trailer": "R1"},  # 合法
                {"kind": "attach", "tractor": "T1", "trailer": "R2"},  # 合法
                {"kind": "attach", "tractor": "T2", "trailer": "R3"},  # 冲突
            ])
        self.assertEqual(c.last_seq(), 1)
        self.assertEqual(c.product_at(1, "T1").stable_identity(), ("T1", "R1"))
        self.assertEqual(c.product_at(1, "T2").stable_identity(), ("T2", None))

    def test_batch_success_is_sequential(self):
        c = build_catalog()
        seqs = c.apply_batch([
            {"kind": "attach", "tractor": "T1", "trailer": "R1"},
            {"kind": "detach", "tractor": "T1", "trailer": "R1"},
            {"kind": "attach", "tractor": "T1", "trailer": "R2"},
        ], batch_id="B1")
        self.assertEqual(seqs, [1, 2, 3])
        self.assertEqual(c.product_at(1, "T1").stable_identity(), ("T1", "R1"))
        self.assertEqual(c.product_at(2, "T1").stable_identity(), ("T1", None))
        self.assertEqual(c.product_at(3, "T1").stable_identity(), ("T1", "R2"))
        # 幂等重放返回同一批序号
        self.assertEqual(
            c.apply_batch([{"kind": "attach", "tractor": "T1", "trailer": "R3"}],
                          batch_id="B1"),
            [1, 2, 3],
        )

    def test_mass_correction_preserves_history_and_offers_corrected_product(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        old = c.product_at(1, "T1")
        seq = c.apply_set_mass("trailer", "R1", 1400, 1500)
        # 旧产品本身不变
        self.assertEqual([(m.lo, m.hi) for m in c.product(old.product_id).masses],
                         [(7000, 7200), (1500, 1600)])
        # 同一挂接的更正版
        corr = c.corrected_product(old)
        self.assertEqual(corr.stable_identity(), ("T1", "R1"))
        self.assertEqual(corr.predecessor, old.product_id)
        self.assertEqual([(m.lo, m.hi) for m in corr.masses],
                         [(7000, 7200), (1400, 1500)])
        # 当前 live 产品等于更正版
        self.assertEqual(c.product_at(seq, "T1").product_id, corr.product_id)
        self.assertEqual(c.verify(), [])

    def test_mass_correction_of_detached_trailer_does_not_touch_other_binding(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        c.apply_set_mass("trailer", "R1", 1400, 1500)
        # R1 的更正版存在，但 live 产品仍应是 R2
        live = c.product_at(c.last_seq(), "T1")
        self.assertEqual(live.stable_identity(), ("T1", "R2"))
        hist = [p for p in c.history("trailer", "R1") if p["mode"] == "RELABEL"]
        self.assertTrue(hist)
        self.assertIn((1400, 1500), [(m["lo"], m["hi"]) for m in hist[-1]["masses"]])
        self.assertEqual(c.verify(), [])

    def test_entity_limits(self):
        c = Catalog()
        for i in range(100):
            c.define_mass("tractor", f"T{i}", 1000, 1100)
        with self.assertRaises(Exception):
            c.define_mass("tractor", "T-extra", 1000, 1100)

    def test_invalid_mass_range_rejected(self):
        c = Catalog()
        with self.assertRaises(CatalogError):
            c.define_mass("tractor", "T1", 5, 4)
        with self.assertRaises(CatalogError):
            c.define_mass("tractor", "T1", -1, 4)

    def test_snapshot_roundtrip(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        snap = c.snapshot_at(1, "T1")
        text = snap.to_json()
        back = Snapshot.from_json(text)
        self.assertEqual(back.to_dict(), snap.to_dict())
        self.assertEqual(back.product.stable_identity(), ("T1", "R1"))


class ExactSolverTests(unittest.TestCase):
    def test_matches_brute_force_over_integer_grid(self):
        # 小整数区间枚举每台实体空重，逐一核对极值。
        bounds = [(7000, 7004), (1500, 1502), (2000, 2003)]
        rows = [
            ([Fraction(1), Fraction(1), Fraction(0)], Fraction(9000)),
            ([Fraction(1), Fraction(0), Fraction(1)], Fraction(12000)),
            ([Fraction(0), Fraction(-1), Fraction(1)], Fraction(3000)),
        ]
        obj = [Fraction(0), Fraction(-1), Fraction(1)]
        ok, lo, hi = _solve_int(bounds, rows, obj)
        vals = []
        for t, a, b in itertools.product(*[range(x, y + 1) for x, y in bounds]):
            if t + a <= 9000 and t + b <= 12000 and -a + b <= 3000:
                vals.append(-a + b)
        self.assertTrue(ok)
        self.assertEqual((lo, hi), (min(vals), max(vals)))


class WarehouseTests(unittest.TestCase):
    def test_same_vehicle_exact_cancellation(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        self.assertTrue(ev.feasible)
        self.assertEqual((ev.net_lo, ev.net_hi), (3000, 3000))
        self.assertEqual((ev.flow_lo, ev.flow_hi), (3000, 3000))
        self.assertEqual(ev.cancellation, "exact cancellation")
        # 空车区间宽度不影响净量
        self.assertTrue(all(c.role == "cancelled" for c in ev.contributions))
        w.confirm(ev.trip_id)
        self.assertEqual(w.trip(ev.trip_id)["status"], STATUS_CONFIRMED)
        self.assertEqual(w.reconcile(), [])

    def test_exact_cancellation_ignores_tare_width(self):
        c = Catalog()
        c.define_mass("tractor", "T", 0, 100000)  # 极宽区间
        c.define_mass("trailer", "R", 0, 100000)
        c.apply_attach("T", "R")
        w = Warehouse(c)
        w.add_ticket(ticket("A", "T", 1, 1, IN, 500))
        w.add_ticket(ticket("B", "T", 1, 2, OUT, 1300))
        ev = w.preview(LOAD, "A", "B")
        self.assertEqual((ev.net_lo, ev.net_hi), (800, 800))

    def test_same_second_multiple_tickets_allowed(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("S1", "T1", 1, 50, IN, 9000))
        w.add_ticket(ticket("S2", "T1", 1, 50, OUT, 12000))
        # 同一秒的两张不同票可组成运输
        ev = w.preview(LOAD, "S1", "S2")
        self.assertTrue(ev.feasible)

    def test_only_trailer_swapped_partial_cancellation_and_integer_extremes(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 3, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        # 枚举全部整数空重，核对精确极值
        flows = []
        for t in range(7000, 7201):
            for r1 in range(1500, 1601):
                for r2 in range(2000, 2201):
                    if t + r1 <= 9000 and t + r2 <= 12000:
                        flow = (12000 - t - r2) - (9000 - t - r1)
                        if flow >= 0:
                            flows.append(flow)
        self.assertTrue(ev.feasible)
        self.assertEqual((ev.net_lo, ev.net_hi), (min(flows), max(flows)))
        # 牵引车系数为 0，两个挂车参与
        roles = {x.label: (x.role, x.coefficient) for x in ev.contributions}
        self.assertEqual(roles["T1"][0], "cancelled")
        self.assertEqual(roles["T1"][1], 0)
        self.assertEqual(roles["R1"][0], "uncertain")
        self.assertEqual(roles["R2"][0], "uncertain")
        # 中点绝不能当结算值
        self.assertNotEqual(ev.net_lo, ev.net_hi)

    def test_unload_only_swap(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 12000))
        w.add_ticket(ticket("K2", "T1", 3, 200, OUT, 9000))
        ev = w.preview(UNLOAD, "K1", "K2")
        flows = []
        for t in range(7000, 7201):
            for r1 in range(1500, 1601):
                for r2 in range(2000, 2201):
                    if t + r1 <= 12000 and t + r2 <= 9000:
                        f = (9000 - t - r2) - (12000 - t - r1)
                        if f <= 0:
                            flows.append(f)
        self.assertTrue(ev.feasible)
        self.assertEqual((ev.net_lo, ev.net_hi), (-max(flows), -min(flows)))

    def test_direction_impossible(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        # LOAD 但离厂比入厂轻
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 12000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 9000))
        ev = w.preview(LOAD, "K1", "K2")
        self.assertFalse(ev.feasible)
        self.assertEqual(ev.reason, REASON_DIRECTION_IMPOSSIBLE)
        self.assertIsNone(ev.net_lo)
        with self.assertRaises(TicketRejected):
            w.confirm(ev.trip_id)

    def test_swapped_direction_impossible_but_region_nonempty(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        w = Warehouse(c)
        # 毛重差很小，方向约束使联合可行域为空
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 3, 200, OUT, 9050))
        ev = w.preview(LOAD, "K1", "K2")
        self.assertFalse(ev.feasible)
        self.assertEqual(ev.reason, REASON_DIRECTION_IMPOSSIBLE)

    def test_ticket_physical_infeasible(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 8000))  # < 最小空车 8500
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        d = w.derive_ticket("K1")
        self.assertFalse(d.feasible)
        self.assertEqual(d.goods_hi, -500)
        ev = w.preview(LOAD, "K1", "K2")
        self.assertEqual(ev.reason, REASON_TICKET_INFEASIBLE)

    def test_time_order_rejected(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 500, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 400, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        self.assertFalse(ev.feasible)
        self.assertEqual(ev.reason, REASON_TIME_ORDER)

    def test_tractor_mismatch_rejected(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_attach("T2", "R2")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T2", 1, 200, OUT, 12000))
        with self.assertRaises(WarehouseError):
            w.preview(LOAD, "K1", "K2")

    def test_ticket_role_mismatch_rejected(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, OUT, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        with self.assertRaises(WarehouseError):
            w.preview(LOAD, "K1", "K2")

    def test_ticket_competition_confirmed_ticket_cannot_be_reused(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        w.add_ticket(ticket("K3", "T1", 1, 300, OUT, 13000))
        t1 = w.preview(LOAD, "K1", "K2")
        t2 = w.preview(LOAD, "K1", "K3")
        w.confirm(t1.trip_id)
        with self.assertRaises(TicketBusy):
            w.confirm(t2.trip_id)
        # 失败确认不改变 t2 状态，也不占票
        self.assertEqual(w.trip(t2.trip_id)["status"], STATUS_DRAFT)
        self.assertEqual(w.ticket_usage("K3")["confirmed_trip"], None)
        self.assertEqual(w.reconcile(), [])

    def test_draft_can_be_superseded_and_reused_without_busy(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        w.add_ticket(ticket("K3", "T1", 1, 300, OUT, 13000))
        t1 = w.preview(LOAD, "K1", "K2")
        w.preview(LOAD, "K1", "K3")
        # 旧草稿未确认，不占票；确认另一个即可
        w.confirm(t1.trip_id)
        self.assertEqual(w.trip(t1.trip_id)["status"], STATUS_CONFIRMED)

    def test_late_catalogue_correction_creates_new_evaluation_with_predecessor(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 3, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        old_bounds = (ev.net_lo, ev.net_hi)
        w.confirm(ev.trip_id)
        # 目录迟到更正 R1
        c.apply_set_mass("trailer", "R1", 1400, 1500)
        re = w.reopen(ev.trip_id)
        self.assertEqual(re.predecessor_trip, ev.trip_id)
        self.assertEqual(re.status, STATUS_REVIEW)
        # 旧确认运输及其评估保持不变
        old = w.evaluation(ev.trip_id)
        self.assertEqual((old.net_lo, old.net_hi), old_bounds)
        self.assertEqual(w.trip(ev.trip_id)["status"], STATUS_CONFIRMED)
        # 票仍归原运输，不被重新记成另一运输
        self.assertEqual(w.ticket_usage("K1")["confirmed_trip"], ev.trip_id)
        self.assertEqual(w.ticket_usage("K2")["confirmed_trip"], ev.trip_id)
        # 重评使用了同一挂接的更正版产品
        self.assertEqual(re.derived_in.product.stable_identity(), ("T1", "R1"))
        self.assertIn((1400, 1500),
                      [(m.lo, m.hi) for m in re.derived_in.product.masses])
        # 简化后的极值复核
        flows = []
        for t in range(7000, 7201):
            for r1 in range(1400, 1501):
                for r2 in range(2000, 2201):
                    if t + r1 <= 9000 and t + r2 <= 12000:
                        f = (12000 - t - r2) - (9000 - t - r1)
                        if f >= 0:
                            flows.append(f)
        self.assertEqual((re.net_lo, re.net_hi), (min(flows), max(flows)))
        self.assertEqual(w.reconcile(), [])

    def test_correction_of_tractor_mass_affects_both_and_still_cancels(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        w.confirm(ev.trip_id)
        c.apply_set_mass("tractor", "T1", 6900, 7100)
        re = w.reopen(ev.trip_id)
        # 同一组合精确抵消，净量仍是毛重差
        self.assertEqual((re.net_lo, re.net_hi), (3000, 3000))
        self.assertEqual((w.evaluation(ev.trip_id).net_lo,
                          w.evaluation(ev.trip_id).net_hi), (3000, 3000))
        self.assertEqual(w.reconcile(), [])

    def test_reopen_history_contains_both_trips(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        w.confirm(ev.trip_id)
        c.apply_set_mass("trailer", "R1", 1550, 1650)
        re = w.reopen(ev.trip_id)
        hist = w.evaluation_history(re.trip_id)
        self.assertEqual([h["trip_id"] for h in hist],
                         [ev.trip_id, re.trip_id])
        self.assertEqual(hist[1]["predecessor_trip"], ev.trip_id)
        # 已确认运输的评估是历史事实，绝不被标记为 superseded
        self.assertIsNone(hist[0]["superseded_by"])

    def test_reopen_repeatedly_keeps_chain(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        cur = w.preview(LOAD, "K1", "K2")
        w.confirm(cur.trip_id)
        for i in range(3):
            c.apply_set_mass("trailer", "R1", 1500 + i, 1600 + i)
            nxt = w.reopen(cur.trip_id)
            self.assertEqual(nxt.predecessor_trip, cur.trip_id)
            cur = nxt
        fam = w.evaluation_history(cur.trip_id)
        self.assertEqual(len(fam), 4)
        self.assertEqual(w.reconcile(), [])

    def test_mass_interval_conflict_between_tickets(self):
        # 同一实体在两票引用到不同且不相交的质量区间 -> 明确冲突
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_set_mass("trailer", "R1", 1800, 1900)
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 2, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        self.assertFalse(ev.feasible)
        self.assertEqual(ev.reason, REASON_MASS_INTERVAL_CONFLICT)

    def test_external_snapshots_only_warehouse(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        snap1 = c.snapshot_at(1, "T1").to_json()
        snap2 = c.snapshot_at(2, "T1").to_json()
        snap3 = c.snapshot_at(3, "T1").to_json()
        # 仓库完全不接触目录
        w = Warehouse(None)
        for s in (snap1, snap2, snap3):
            w.register_snapshot(s)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 3, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        self.assertTrue(ev.feasible)
        self.assertEqual(ev.derived_in.product.stable_identity(), ("T1", "R1"))
        self.assertEqual(ev.derived_out.product.stable_identity(), ("T1", "R2"))
        self.assertEqual(w.reconcile(), [])

    def test_external_snapshot_no_guessing_from_current_binding(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        # 只导入 seq 3 的快照；seq 1 的票没有任何产品可引用
        w = Warehouse(None)
        w.register_snapshot(c.snapshot_at(3, "T1").to_json())
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 3, 200, OUT, 12000))
        with self.assertRaises(WarehouseError):
            w.preview(LOAD, "K1", "K2")

    def test_conflicting_external_snapshot_rejected(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        snap = c.snapshot_at(1, "T1")
        data = snap.to_dict()
        w = Warehouse(None)
        w.register_snapshot(data)
        tampered = json.loads(json.dumps(data))
        tampered["product"]["masses"][0]["lo"] = 1
        with self.assertRaises(WarehouseError):
            w.register_snapshot(tampered)

    def test_preview_does_not_reserve_tickets_confirm_does(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        self.assertEqual(w.ticket_usage("K1")["confirmed_trip"], None)
        w.confirm(ev.trip_id)
        self.assertEqual(w.ticket_usage("K1")["confirmed_trip"], ev.trip_id)

    def test_confirm_is_idempotent(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        w.confirm(ev.trip_id)
        again = w.confirm(ev.trip_id)
        self.assertEqual(again.status, STATUS_CONFIRMED)

    def test_ticket_limits(self):
        c = build_catalog()
        w = Warehouse(c)
        for i in range(1000):
            w.add_ticket(ticket(f"T{i:04d}", "T1", 0, i, IN, 9000))
        with self.assertRaises(WarehouseError):
            w.add_ticket(ticket("TOVER", "T1", 0, 1, IN, 9000))

    def test_trip_limits(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        for i in range(250):
            w.add_ticket(ticket(f"I{i:04d}", "T1", 1, 2 * i, IN, 9000))
            w.add_ticket(ticket(f"O{i:04d}", "T1", 1, 2 * i + 1, OUT, 12000))
        for i in range(200):
            w.preview(LOAD, f"I{i:04d}", f"O{i:04d}")
        with self.assertRaises(WarehouseError):
            w.preview(LOAD, "I0200", "O0200")

    def test_idempotent_ticket_add(self):
        c = build_catalog()
        w = Warehouse(c)
        t = ticket("K1", "T1", 0, 1, IN, 9000)
        w.add_ticket(t)
        w.add_ticket(t)  # 相同数据可重复
        with self.assertRaises(WarehouseError):
            w.add_ticket({**t, "gross": 9001})

    def test_explain_ticket_and_trip(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        ex = w.explain_ticket("K1")
        self.assertEqual(ex["tare"], [8500, 8800])
        self.assertEqual(ex["goods"], [200, 500])
        self.assertEqual([e["label"] for e in ex["entities"]], ["T1", "R1"])
        ex_trip = w.explain_trip(ev.trip_id)
        self.assertEqual(ex_trip["net"], [3000, 3000])
        self.assertEqual(ex_trip["ticket_usage"]["IN"]["ticket_id"], "K1")

    def test_catalog_query_at_arbitrary_seq(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")     # seq 1
        c.apply_detach("T1", "R1")     # seq 2
        c.apply_attach("T1", "R2")     # seq 3
        c.apply_set_mass("trailer", "R1", 1400, 1500)  # seq 4
        # 任意序号都返回该处已生效的 live 产品
        self.assertEqual(c.product_at(0, "T1").stable_identity(), ("T1", None))
        self.assertEqual(c.product_at(1, "T1").stable_identity(), ("T1", "R1"))
        self.assertEqual(c.product_at(2, "T1").stable_identity(), ("T1", None))
        self.assertEqual(c.product_at(3, "T1").stable_identity(), ("T1", "R2"))
        self.assertEqual(c.product_at(4, "T1").stable_identity(), ("T1", "R2"))
        self.assertEqual(c.product_at(100, "T1").stable_identity(), ("T1", "R2"))

    def test_correction_after_detach_no_id_collision(self):
        # detach 与历史更正版在同一 seq 都要落产品时，ID 必须唯一。
        c = build_catalog()
        c.apply_attach("T1", "R1")
        att = c.product_at(1, "T1")
        c.apply_detach("T1", "R1")
        c.apply_set_mass("tractor", "T1", 6950, 7150)
        self.assertEqual(c.verify(), [])
        # 当前 live 仍是空载 (detach 后)
        self.assertEqual(c.product_at(c.last_seq(), "T1").stable_identity(), ("T1", None))
        # 原挂接的更正版保留 R1 身份与新的牵引车区间
        corr = c.corrected_product(att)
        self.assertEqual(corr.stable_identity(), ("T1", "R1"))
        self.assertEqual([(m.lo, m.hi) for m in corr.masses],
                         [(6950, 7150), (1500, 1600)])

    def test_corrected_product_as_of_seq(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        orig = c.product_at(1, "T1")
        c.apply_set_mass("trailer", "R1", 1400, 1500)  # seq 2
        before = c.corrected_product(orig, as_of_seq=1)
        self.assertEqual(before.product_id, orig.product_id)
        after = c.corrected_product(orig)
        self.assertIn((1400, 1500), [(m.lo, m.hi) for m in after.masses])

    def test_batched_attach_then_correct(self):
        c = build_catalog()
        seqs = c.apply_batch([
            {"kind": "attach", "tractor": "T1", "trailer": "R1"},
            {"kind": "set_mass", "entity": ("trailer", "R1"), "lo": 1520, "hi": 1580},
        ], batch_id="B")
        self.assertEqual(seqs, [1, 2])
        self.assertEqual([(m.lo, m.hi) for m in c.product_at(1, "T1").masses],
                         [(7000, 7200), (1500, 1600)])
        self.assertEqual([(m.lo, m.hi) for m in c.product_at(2, "T1").masses],
                         [(7000, 7200), (1520, 1580)])
        self.assertEqual(c.verify(), [])

    def test_correction_to_detached_trailer_then_reopen_uses_corrected_history(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 3, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        w.confirm(ev.trip_id)
        c.apply_set_mass("tractor", "T1", 6950, 7150)  # 换挂之后更正牵引车
        re = w.reopen(ev.trip_id)
        # 共享牵引车仍精确抵消，两个挂车共同参与，结果按新区间重算
        self.assertTrue(re.feasible)
        roles = {x.label: x for x in re.contributions}
        self.assertEqual(roles["T1"].coefficient, 0)
        self.assertEqual((roles["T1"].lo, roles["T1"].hi), (6950, 7150))
        self.assertEqual(w.reconcile(), [])

    def test_attachment_history_correction_reevaluates_on_new_products(self):
        # 挂接历史更正：事后发现当时挂的是 R2 而不是 R1。
        # 只能在目录追加新动作形成新产品，再用显式 product_seqs 重评。
        c = build_catalog()
        c.apply_attach("T1", "R1")          # seq 1（当时记录）
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        w.confirm(ev.trip_id)
        # 追加：更正历史为 T1 挂 R2
        c.apply_detach("T1", "R1")          # seq 2
        corrected_seq = c.apply_attach("T1", "R2")  # seq 3
        # 重评：两张票都改引用更正后的序号
        re = w.reevaluate(
            ev.trip_id,
            product_seqs={IN: corrected_seq, OUT: corrected_seq},
        )
        self.assertEqual(re.predecessor_trip, ev.trip_id)
        self.assertEqual(re.derived_in.product.stable_identity(), ("T1", "R2"))
        self.assertEqual(re.derived_out.product.stable_identity(), ("T1", "R2"))
        # 组合相同 -> 精确抵消，且是毛重差
        self.assertEqual((re.net_lo, re.net_hi), (3000, 3000))
        # 旧确认运输不受影响
        self.assertEqual(w.evaluation(ev.trip_id).net_lo, 3000)
        self.assertEqual(w.ticket_usage("K1")["confirmed_trip"], ev.trip_id)
        self.assertEqual(w.reconcile(), [])

    def test_reconcile_detects_tampered_evaluation(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 1, 200, OUT, 12000))
        ev = w.preview(LOAD, "K1", "K2")
        # 篡改评估上界
        w.conn.execute(
            "UPDATE evaluation SET net_hi=? WHERE trip_id=?", (9999, ev.trip_id)
        )
        w.conn.commit()
        errs = w.reconcile()
        self.assertTrue(any("not reproducible" in e for e in errs), errs)

    def test_preview_with_explicit_product_seqs(self):
        c = build_catalog()
        c.apply_attach("T1", "R1")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        w = Warehouse(c)
        w.add_ticket(ticket("K1", "T1", 1, 100, IN, 9000))
        w.add_ticket(ticket("K2", "T1", 3, 200, OUT, 12000))
        # 显式让两张票都按 seq 3 的组合（相同 identity）计算
        ev = w.preview(LOAD, "K1", "K2",
                       product_seqs={IN: 3, OUT: 3})
        self.assertEqual(ev.derived_in.product.stable_identity(), ("T1", "R2"))
        self.assertEqual(ev.derived_out.product.stable_identity(), ("T1", "R2"))
        self.assertEqual((ev.net_lo, ev.net_hi), (3000, 3000))
        self.assertEqual((ev.derived_in.product_seq, ev.derived_out.product_seq), (3, 3))


if __name__ == "__main__":
    unittest.main()
