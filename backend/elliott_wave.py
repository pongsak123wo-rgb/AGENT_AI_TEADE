"""Elliott Wave analysis — real zigzag pivot detection + rule/ratio
checks on real price data, not a fabricated wave count.

Honesty check up front: Elliott Wave counting is inherently subjective —
even professional analysts disagree on the count of the same chart. This
module can't escape that. What it CAN do honestly is:
1. Find real swing pivots via a zigzag filter (no invented points)
2. Test the most recent 5 legs against the 3 hard Elliott rules
3. Score how well they fit, instead of asserting "this IS wave 3"
   unconditionally — confidence is reported, not hidden.

Treat the output as "if this is an impulse, here's where it likely is
and how confident the rule-fit is" — one more weighted opinion for the
LLM to reason with alongside SMC/indicators, not a ground truth oracle.
"""
from __future__ import annotations


def _zigzag(prices: list[float], threshold_pct: float = 0.3) -> list[tuple[int, float]]:
    """Filters out moves smaller than threshold_pct% to keep only the
    significant swings — same idea as charting software's zigzag
    indicator. Returns [(index, price), ...] alternating direction."""
    if len(prices) < 3:
        return [(0, prices[0])] if prices else []

    pivots = []
    trend = None
    last_idx, last_extreme = 0, prices[0]

    for i in range(1, len(prices)):
        change = (prices[i] - last_extreme) / last_extreme

        if trend is None:
            if abs(change) >= threshold_pct / 100:
                trend = "up" if change > 0 else "down"
                last_idx, last_extreme = i, prices[i]
        elif trend == "up":
            if prices[i] >= last_extreme:
                last_idx, last_extreme = i, prices[i]
            elif (last_extreme - prices[i]) / last_extreme >= threshold_pct / 100:
                pivots.append((last_idx, last_extreme))
                trend = "down"
                last_idx, last_extreme = i, prices[i]
        else:  # trend == "down"
            if prices[i] <= last_extreme:
                last_idx, last_extreme = i, prices[i]
            elif (prices[i] - last_extreme) / last_extreme >= threshold_pct / 100:
                pivots.append((last_idx, last_extreme))
                trend = "up"
                last_idx, last_extreme = i, prices[i]

    pivots.append((last_idx, last_extreme))
    return pivots


def _check_impulse_rules(legs: list[float], up: bool) -> dict:
    """legs = [wave1, wave2, wave3, wave4, wave5] as signed price
    distances (all positive magnitudes here, direction implied by `up`).
    Tests the 3 rules that make an Elliott impulse valid, plus 2 common
    Fibonacci guideline ratios for extra confidence (not hard rules)."""
    w1, w2, w3, w4, w5 = legs
    checks = {
        "wave2_not_beyond_wave1_start": w2 < w1,
        "wave3_is_longest": w3 >= w1 and w3 >= w5,
        "wave4_no_overlap_wave1": w4 < w3,  # approximation without absolute price levels
    }
    hard_rules_passed = sum(checks.values())

    fib_checks = {
        "wave3_extended_1.618x_wave1": 1.3 <= (w3 / w1 if w1 else 0) <= 2.2,
        "wave4_retrace_23_to_50pct_wave3": 0.15 <= (w4 / w3 if w3 else 0) <= 0.6,
    }
    fib_passed = sum(fib_checks.values())

    confidence = round((hard_rules_passed / 3 * 70) + (fib_passed / 2 * 30), 1)
    return {"checks": checks, "fib_checks": fib_checks, "confidence": confidence}


def analyze_elliott(prices: list[float], threshold_pct: float = 0.15) -> dict:
    if len(prices) < 30:
        return {"ready": False, "reason": "ต้องมีอย่างน้อย 30 จุดราคาสำหรับหา pivot พอจะนับคลื่น"}

    pivots = _zigzag(prices, threshold_pct=threshold_pct)
    if len(pivots) < 6:
        return {"ready": False, "reason": f"พบ pivot สำคัญแค่ {len(pivots)} จุด (ต้องการ 6 ขึ้นไปสำหรับนับคลื่น 1-5)"}

    last6 = pivots[-6:]
    legs = [abs(last6[i + 1][1] - last6[i][1]) for i in range(5)]
    overall_up = last6[-1][1] > last6[0][1]

    fit = _check_impulse_rules(legs, up=overall_up)

    wave_labels = ["0", "1", "2", "3", "4", "5"]
    labeled_pivots = [
        {"idx": idx, "price": round(price, 5), "wave": wave_labels[i]}
        for i, (idx, price) in enumerate(last6)
    ]

    # Projection: if the current incomplete leg from pivot 5 (index -1) is
    # wave 5 still developing, project wave5 ≈ wave1 length from the wave4
    # low/high (classic equality guideline, just one of several).
    wave1_len = legs[0]
    wave4_point = last6[4][1]
    if overall_up:
        wave5_target = round(wave4_point + wave1_len, 5)
    else:
        wave5_target = round(wave4_point - wave1_len, 5)

    return {
        "ready": True,
        "direction": "up" if overall_up else "down",
        "pivots": labeled_pivots,
        "rule_fit_confidence": fit["confidence"],
        "rule_checks": fit["checks"],
        "fib_checks": fit["fib_checks"],
        "current_position": "ปลายคลื่น 5 (กำลังเกิด) หรือเริ่ม correction A-B-C ถ้าโครงสร้างคลื่น 1-5 ข้างบนแม่นจริง",
        "wave5_projection": wave5_target,
        "caveat": "การนับคลื่น Elliott เป็น subjective โดยธรรมชาติ — ใช้ rule_fit_confidence ประกอบการตัดสินใจ ไม่ใช่ความจริงสมบูรณ์",
    }
