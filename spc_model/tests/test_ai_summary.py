"""AI 摘要資料脈絡的測試。

執行：在 spc_model 資料夾下 `python -m unittest discover -s tests`
"""

import unittest

from app.ai_summary import build_summary_context, build_summary_prompt


def _point(x, value, rules=(), spec=False):
    rules = list(rules)
    return {
        "x": str(x),
        "value": value,
        "is_out_of_spec": spec,
        "is_out_of_control": any(
            not rule.endswith(("usl", "lsl")) for rule in rules
        ),
        "violated_rules": rules,
    }


NELSON_UP = "nelson_8_points_above_center"


def _demo_chart(ucl=10.0288):
    """對應 DEMO-A 外徑 #1~30：#21–#29 只違反連串規則，#30 超出 UCL 與 USL。"""
    points = [_point(i, 10.0) for i in range(1, 21)]
    points += [_point(i, 10.01, [NELSON_UP]) for i in range(21, 30)]
    points.append(
        _point(30, 10.06, ["above_usl", "above_ucl", NELSON_UP], spec=True)
    )
    limits = {"cl": 9.9997, "ucl": ucl, "lcl": 9.9706, "usl": 10.05, "lsl": 9.95}
    return {
        "feature_name": "外徑",
        "chart_type": "I-MR",
        "limits": limits,
        "points": points,
        "primary_chart": {"component_type": "I", "limits": limits, "points": points},
        "secondary_chart": {
            "component_type": "MR",
            "limits": {"cl": 0.0109, "ucl": 0.0357, "lcl": 0.0},
            "points": [_point(i, 0.005) for i in range(2, 30)]
            + [_point(30, 0.0457, ["above_ucl"])],
        },
    }


def _capability(cp=2.05224, cpk=2.04938, mean=10.0001):
    return {
        "metrics": {
            "feature_name": "外徑",
            "cp": cp,
            "cpk": cpk,
            "cpm": cp,
            "cpmk": cpk,
            "ppk": cpk,
            "mean": mean,
            "sigma": 0.0081,
            "usl": 10.05,
            "lsl": 9.95,
            "nominal_value": 10.0,
            "sample_size": 23,
        },
        "cpk_threshold": 1.33,
    }


class SummaryContextTests(unittest.TestCase):
    def test_run_rule_points_are_not_counted_as_beyond_control_limits(self):
        context = build_summary_context(_demo_chart(), _capability())

        self.assertIn("超出管制界限：1 點（#30 超出 UCL）", context)
        self.assertIn("違反連串規則、但仍在管制界限內：9 點（#21–#29）", context)
        self.assertIn("超出規格：1 點（#30 超出 USL）", context)
        self.assertIn("上圖：#30 超出管制上限 UCL", context)
        self.assertIn("下圖：#30 超出管制上限 UCL，代表相鄰兩筆量測值之間的變動突然變大", context)
        # 方向要由程式寫清楚，不能留「UCL/LCL」讓模型照抄
        self.assertNotIn("UCL/LCL", context)
        self.assertIn(
            "系統處於 Phase II 監控中，但製程目前出現失控訊號，且有 1 點超規（#30，屬不良品）",
            context,
        )
        self.assertNotIn("未處於統計管制狀態", context)
        self.assertIn("從 #21 起多點持續落在中心線上方", context)

    def test_centered_process_does_not_suggest_recentering(self):
        context = build_summary_context(_demo_chart(), _capability())

        self.assertIn("Cpk 與 Cp 幾乎相同（Cpk/Cp = 1.00）", context)
        # 判讀要是完整句子，不能有讓模型照抄的欄位標籤
        self.assertNotIn("置中程度：", context)
        self.assertIn("不需要為了提高 Cpk 而調整製程中心", context)
        self.assertNotIn("建議調整製程中心", context)
        # 數值四捨五入到兩位，避免模型照抄 2.05224 這種長數字
        self.assertIn("Cp 2.05、Cpk 2.05", context)
        self.assertNotIn("2.05224", context)

    def test_off_center_process_names_the_side(self):
        context = build_summary_context(
            _demo_chart(), _capability(cp=2.0, cpk=1.2, mean=10.02)
        )

        self.assertIn("Cpk/Cp = 0.60", context)
        self.assertIn("偏向規格上限", context)
        self.assertIn("低於門檻", context)

    def test_capability_is_marked_as_reference_when_out_of_control(self):
        context = build_summary_context(_demo_chart(), _capability())

        self.assertIn("能力指標只代表正常樣本的表現，僅供參考", context)
        self.assertIn("計算樣本：23 筆（管制圖共 30 點", context)

    def test_phase_one_does_not_judge_stability(self):
        chart = _demo_chart()
        for key in ("cl", "ucl", "lcl"):
            chart["primary_chart"]["limits"][key] = None
        context = build_summary_context(chart, _capability())

        self.assertIn("系統處於 Phase I（尚未建立管制界線）", context)
        self.assertNotIn("失控訊號", context)

    def test_in_control_process_says_no_signal(self):
        chart = _demo_chart()
        for series in (chart["primary_chart"], chart["secondary_chart"]):
            series["points"] = [
                {**p, "violated_rules": [], "is_out_of_control": False, "is_out_of_spec": False}
                for p in series["points"]
            ]
        context = build_summary_context(chart, _capability())

        self.assertIn("系統處於 Phase II 監控中，製程目前沒有失控訊號", context)

    def test_missing_capability_is_explicit(self):
        context = build_summary_context(_demo_chart(), None)

        self.assertIn("【製程能力】未提供，不可推測任何能力指標", context)

    def test_prompt_contains_context_and_terminology_rules(self):
        context = build_summary_context(_demo_chart(), _capability())
        prompt = build_summary_prompt(context)

        self.assertIn(context, prompt)
        self.assertIn("不可說成「超出管制界限」", prompt)
        self.assertIn("第一句寫出 Cpk 數值、門檻數值", prompt)
        self.assertIn("只有 Cpk 有門檻", prompt)
        self.assertIn("【建議處置】", prompt)

    def test_findings_checklist_includes_shift_start(self):
        context = build_summary_context(_demo_chart(), _capability())
        checklist = context.split("【異常重點清單】")[1].split("【製程能力】")[0]

        self.assertIn("1. 上圖：從 #21 起多點持續落在中心線上方", checklist)
        self.assertIn("上圖：#30 超出管制上限 UCL", checklist)
        self.assertIn("上圖：#30 超出規格上限 USL", checklist)
        self.assertIn("下圖：#30 超出管制上限 UCL", checklist)

    def test_recommendations_start_from_shift_point(self):
        context = build_summary_context(_demo_chart(), _capability())
        recs = context.split("【建議處置清單】")[1]

        self.assertIn("調查從 #21 起製程平均值往上偏移，以及 #30 超出管制界限的原因", recs)
        self.assertIn("隔離並確認 #30 工件的處置", recs)
        self.assertNotIn("調整製程中心", recs)

    def test_off_center_recommends_recentering(self):
        context = build_summary_context(
            _demo_chart(), _capability(cp=2.0, cpk=1.2, mean=10.02)
        )
        recs = context.split("【建議處置清單】")[1]

        self.assertIn("建議調整製程中心", recs)

    def test_threshold_applies_only_to_cpk(self):
        context = build_summary_context(_demo_chart(), _capability())

        self.assertIn("Cpk 為 2.05，高於門檻 1.33，達到門檻。", context)
        self.assertIn("門檻只適用於 Cpk", context)


if __name__ == "__main__":
    unittest.main()
