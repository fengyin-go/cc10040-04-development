"""报告取数覆盖视图测试。

验证编制人员核对场景：
1. 多指标报告生成时单章模板失败不影响其他章节；
2. 齐备 / 无记录 / 无法执行 三态互斥，空结果与取数失败分清；
3. 章节汇总只认真实样本，缺项不补零；
4. 同一份稿件可反复打开，补取后可引用章节发生变化。
"""
from __future__ import annotations

import pytest

from app.services.query_builder import (
    OUTCOME_EMPTY,
    OUTCOME_FAILED,
    OUTCOME_HAS_ROWS,
    QueryBuilderService,
)
from app.services.report_template import (
    COVERAGE_BLOCKED,
    COVERAGE_EMPTY,
    COVERAGE_READY,
    ReportTemplateService,
)
from app.store import store


def _make_scenario(prefix: int = 500) -> dict[str, int]:
    """构造互相独立的三章节报告场景，返回各实体 ID。

    章节A（齐备）：有指标行 + 生效口径 + 已发布查询模板。
    章节B（无记录）：无指标行 + 生效口径 + 已发布查询模板（执行成功但空）。
    章节C（无法执行）：无生效口径 + 已发布查询模板。
    """
    metric_a, metric_b, metric_c = f"覆盖指标A_{prefix}", f"覆盖指标B_{prefix}", f"覆盖指标C_{prefix}"
    metric_rows = store.rows("metric_definition")
    caliber_rows = store.rows("caliber_version")
    query_rows = store.rows("query_builder")
    report_rows = store.rows("report_template")

    metric_rows.append({
        "id": prefix + 1, "status": "已发布", "pending": False, "abnormal": False,
        "指标名称": metric_a, "计算口径": "演示口径", "统计粒度": "天",
        "数据源": "demo_dw", "业务域": "演示域",
    })
    caliber_rows.append({
        "id": prefix + 1, "status": "已生效", "pending": False, "abnormal": False,
        "所属指标": metric_a, "版本号": f"v-{prefix}-a", "口径描述": "A 章生效口径",
        "变更类型": "初始创建", "生效时间": "2026-10-01",
    })
    caliber_rows.append({
        "id": prefix + 2, "status": "已生效", "pending": False, "abnormal": False,
        "所属指标": metric_b, "版本号": f"v-{prefix}-b", "口径描述": "B 章生效口径",
        "变更类型": "初始创建", "生效时间": "2026-10-01",
    })
    caliber_rows.append({
        "id": prefix + 3, "status": "待生效", "pending": True, "abnormal": False,
        "所属指标": metric_c, "版本号": f"v-{prefix}-c", "口径描述": "C 章口径待生效",
        "变更类型": "初始创建", "生效时间": "2026-12-01",
    })
    for offset, metric in enumerate((metric_a, metric_b, metric_c), start=1):
        query_rows.append({
            "id": prefix + offset, "status": "已发布", "pending": False, "abnormal": False,
            "模板名称": f"演示查询_{prefix}_{offset}", "关联指标": metric,
            "维度组合": "渠道", "过滤条件": "", "缓存策略": "短期缓存",
        })
    report_rows.append({
        "id": prefix + 9, "status": "已发布", "pending": False, "abnormal": False,
        "模板名称": f"演示报告_{prefix}",
        "包含指标集": f"{metric_a},{metric_b},{metric_c}",
        "报告周期": "2026年第四季度", "输出格式": "HTML", "分发渠道": "演示",
    })
    return {
        "report_id": prefix + 9,
        "query_a": prefix + 1,
        "query_b": prefix + 2,
        "query_c": prefix + 3,
        "caliber_c": prefix + 3,
        "metric_c": metric_c,
    }


class TestQueryExecutionRuns:
    """查询构建器必须留下实际执行的样本与口径。"""

    def test_failure_is_recorded_without_samples(self):
        ids = _make_scenario(510)
        result = QueryBuilderService().execute_query(ids["query_c"], operator="测试员")
        assert "error" in result
        run = store.latest_query_run(ids["query_c"])
        assert run is not None
        assert run["outcome"] == OUTCOME_FAILED
        assert run["sample_rows"] == []
        assert run["caliber_version"] is None
        assert "没有已生效的口径版本" in run["error"]

    def test_empty_result_distinct_from_failure(self):
        ids = _make_scenario(520)
        result = QueryBuilderService().execute_query(ids["query_b"], operator="测试员")
        assert "error" not in result
        assert result["rows"] == []
        run = store.latest_query_run(ids["query_b"])
        assert run["outcome"] == OUTCOME_EMPTY
        assert run["row_count"] == 0
        assert run["sample_rows"] == []
        assert run["caliber_version"]  # 口径版本仍然留痕

    def test_success_keeps_sample_and_caliber(self):
        ids = _make_scenario(530)
        result = QueryBuilderService().execute_query(ids["query_a"], operator="测试员")
        assert result["rows"], "章节A应有真实样本"
        run = store.latest_query_run(ids["query_a"])
        assert run["outcome"] == OUTCOME_HAS_ROWS
        assert run["row_count"] == len(result["rows"])
        assert len(run["sample_rows"]) <= 3
        assert run["sample_rows"][0]["口径版本"] == run["caliber_version"]
        # 同次查询的样本与版本一致
        assert all(row["口径版本"] == run["caliber_version"] for row in result["rows"])


class TestReportCoverageView:
    """报告取数覆盖视图：三态、缺项、样本口径聚合。"""

    def test_coverage_three_states_in_one_report(self):
        ids = _make_scenario(540)
        svc = ReportTemplateService()
        draft = svc.generate_report(ids["report_id"], operator="编制人小王")
        assert "error" not in draft
        by_metric = {s["metric"]: s for s in draft["sections"]}
        coverages = {s["coverage"] for s in draft["sections"]}
        assert coverages == {COVERAGE_READY, COVERAGE_EMPTY, COVERAGE_BLOCKED}

        ready = next(s for s in draft["sections"] if s["coverage"] == COVERAGE_READY)
        empty = next(s for s in draft["sections"] if s["coverage"] == COVERAGE_EMPTY)
        blocked = next(s for s in draft["sections"] if s["coverage"] == COVERAGE_BLOCKED)

        # 齐备章节有真实样本和口径
        assert ready["sample_rows"], "齐备章节必须带实际样本"
        assert ready["caliber_version"]
        assert ready["chart_ready"] is True
        # 无记录章节：执行成功但零行，不能补零
        assert empty["sample_rows"] == []
        assert empty["usable_row_count"] == 0
        assert any("不得补零" in item for item in empty["missing_items"])
        # 无法执行章节：失败原因可核对
        assert blocked["sample_rows"] == []
        assert any("口径" in item for item in blocked["missing_items"])
        assert blocked["chart_ready"] is False

    def test_summary_never_zero_fills_missing(self):
        ids = _make_scenario(550)
        svc = ReportTemplateService()
        draft = svc.generate_report(ids["report_id"])
        summary = draft["coverage_view"]["summary"]
        assert summary["total_sections"] == 3
        assert summary["ready_sections"] == 1
        assert summary["empty_sections"] == 1
        assert summary["blocked_sections"] == 1
        assert summary["usable_charts"] == 1  # 缺项章节不产生图表，更不补零
        # 可引用图表清单只来自齐备章节
        usable = [t for s in draft["sections"] for t in s["templates"]
                  if t["outcome"] == OUTCOME_HAS_ROWS]
        assert len(usable) == summary["usable_charts"]

    def test_partial_chapter_failure_keeps_others_verifiable(self):
        ids = _make_scenario(560)
        svc = ReportTemplateService()
        draft = svc.generate_report(ids["report_id"])
        # C 章失败不应中断报告，A/B 章各自带独立可核对数据
        assert draft["total_metrics"] == 3
        independent = [s for s in draft["sections"] if s["coverage"] != COVERAGE_BLOCKED]
        assert {s["metric"] for s in independent}
        for sec in independent:
            assert sec["templates"], "每章都保留各自查询模板的独立留痕"

    def test_coverage_view_without_execution_marks_missing(self):
        ids = _make_scenario(570)
        view = ReportTemplateService().build_coverage_view(ids["report_id"])
        # 未执行过：全部章节都应提示无成功留痕，且不伪造样本
        for sec in view["sections"]:
            assert sec["sample_rows"] == []
            assert any("留痕" in item or "口径" in item or "模板" in item
                       for item in sec["missing_items"])


class TestDraftReopenAndRefetch:
    """生成 → 打开核对 → 补取 → 重新打开同一稿件。"""

    def test_reopen_same_draft_reflects_latest_runs(self):
        ids = _make_scenario(580)
        svc = ReportTemplateService()
        draft = svc.generate_report(ids["report_id"], operator="编制人小李")
        draft_id = draft["draft_id"]

        reopened = svc.open_draft(draft_id)
        assert "error" not in reopened
        assert reopened["draft_id"] == draft_id
        assert reopened["coverage_view"]["summary"]["blocked_sections"] == 1

    def test_refetch_blocked_chapter_after_caliber_activated(self):
        ids = _make_scenario(590)
        svc = ReportTemplateService()
        draft = svc.generate_report(ids["report_id"])
        draft_id = draft["draft_id"]
        before = {s["metric"]: s["coverage"] for s in draft["sections"]}
        assert before[ids["metric_c"]] == COVERAGE_BLOCKED

        # 补取前置条件尚不足（无生效口径）：补取不产生新成功记录
        first = svc.refetch_missing(ids["report_id"], draft_id=draft_id)
        c_run = [r for r in first["refetch_results"] if r["metric"] == ids["metric_c"]]
        assert c_run == []

        # 启用 C 章口径后补取
        caliber = store.find("caliber_version", ids["caliber_c"])
        caliber["status"] = "已生效"
        # 给 C 章补一条指标登记行，使其取数得到真实样本
        store.rows("metric_definition").append({
            "id": ids["caliber_c"] + 1000, "status": "已发布",
            "pending": False, "abnormal": False,
            "指标名称": ids["metric_c"], "计算口径": "履约演示口径",
            "统计粒度": "天", "数据源": "demo_dw", "业务域": "演示域",
        })

        second = svc.refetch_missing(ids["report_id"], operator="编制人小李",
                                     draft_id=draft_id)
        c_result = next(r for r in second["refetch_results"]
                        if r["metric"] == ids["metric_c"])
        assert c_result["outcome"] == OUTCOME_HAS_ROWS
        assert c_result["row_count"] > 0

        # 重新打开同一稿件：C 章已可引用，其他章节状态不变
        reopened = svc.open_draft(draft_id)
        after = {s["metric"]: s["coverage"] for s in reopened["coverage_view"]["sections"]}
        assert after[ids["metric_c"]] == COVERAGE_READY
        assert reopened["coverage_view"]["summary"]["ready_sections"] == 2
        assert reopened["coverage_view"]["summary"]["blocked_sections"] == 0

    def test_refetch_empty_chapter_stays_distinct_from_zero(self):
        ids = _make_scenario(600)
        svc = ReportTemplateService()
        svc.generate_report(ids["report_id"])
        # B 章始终无指标登记行：补取后仍是「成功无记录」，不得翻成零值数据
        result = svc.refetch_missing(ids["report_id"])
        b_run = next(r for r in result["refetch_results"]
                     if r["query_template_id"] == ids["query_b"])
        assert b_run["outcome"] == OUTCOME_EMPTY
        section = next(s for s in result["coverage_view"]["sections"]
                       if s["coverage"] == COVERAGE_EMPTY)
        assert section["usable_row_count"] == 0
        assert section["sample_rows"] == []

    def test_draft_listing_and_seed_scenario_end_to_end(self):
        # 种子数据内置的《季度经营分析报告》覆盖三态
        svc = ReportTemplateService()
        draft = svc.generate_report(101, operator="经营分析员")
        assert "error" not in draft
        names = [s["metric"] for s in draft["sections"]]
        assert names == ["营收指标", "风控指标", "履约指标"]
        assert {s["coverage"] for s in draft["sections"]} == {
            COVERAGE_READY, COVERAGE_EMPTY, COVERAGE_BLOCKED}
        listed = svc.list_drafts(101)
        assert any(d["draft_id"] == draft["draft_id"] for d in listed)
