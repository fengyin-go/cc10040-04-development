"""报告模板领域服务：管理定期分析报告的模板、指标组合和自动分发。

跨模块数据流: pivot_table -> report_template
             metric_definition -> query_builder -> report_template（包含指标查询结果）

报告取数视图（面向编制人员）：
按报告指标集逐章聚合各查询模板实际执行所用的样本、口径版本和取数缺项。
章节状态三态互斥：
  - 齐备：至少一个查询模板成功取到数据，可直接引用图表
  - 无记录：模板可执行且同次查询成功，但结果为空（缺项，汇总不得补零）
  - 无法执行：所有模板都没有成功取数（未发布/无生效口径/执行失败）
一份多指标报告生成时允许部分章节失败，已成功章节的数据独立可核对。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app.services.query_builder import (
    OUTCOME_EMPTY,
    OUTCOME_FAILED,
    OUTCOME_HAS_ROWS,
    QueryBuilderService,
)

# 章节取数覆盖三态
COVERAGE_READY = "齐备"
COVERAGE_EMPTY = "无记录"
COVERAGE_BLOCKED = "无法执行"

# 汇总只认真实取到的样本行；缺项（空/失败）一律不补零。
SAMPLE_ROW_LIMIT = 3


class ReportTemplateService(BaseModuleService):
    MODULE = "report_template"
    ENTITY = "报告模板"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["模板名称", "包含指标集", "报告周期", "输出格式", "分发渠道"]
    REQUIRED_FIELDS = ["模板名称", "包含指标集", "报告周期"]
    STATUS_ORDER = ["草稿", "已发布", "已停用"]
    ACTION_RULES = {"发布模板": "已发布", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    # ── 取数覆盖视图 ──

    def _build_template_coverage(self, query_row: dict[str, Any],
                                 builder: QueryBuilderService,
                                 operator: str, execute: bool) -> dict[str, Any]:
        """单个查询模板的取数情况：优先看实际执行留痕，必要时现场执行一次。"""
        template_id = int(query_row.get("id", 0))
        published = query_row.get("status") == "已发布"
        latest = store.latest_query_run(template_id)

        if execute and published:
            # 生成/补取时实际跑一次，留痕由 execute_query 写入；
            # 失败（如无生效口径）也会留下「执行失败」记录。
            result = builder.execute_query(template_id, operator=operator)
            latest = store.latest_query_run(template_id)
            execution_error = result.get("error", "") if isinstance(result, dict) and "error" in result else ""
        else:
            execution_error = ""

        calibers = store.find_by_field(
            "caliber_version", "所属指标", query_row.get("关联指标", ""))
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)

        missing_items: list[str] = []
        if not published:
            missing_items.append(f"查询模板未发布（当前状态：{query_row.get('status')}），不可取数")
        if active_caliber is None:
            missing_items.append("指标没有已生效的口径版本")
        if latest is None:
            missing_items.append("查询模板尚无成功执行留痕，未取得任何样本")
        elif latest.get("outcome") == OUTCOME_FAILED:
            missing_items.append(f"最近一次取数执行失败：{latest.get('error') or '未知原因'}")
        elif latest.get("outcome") == OUTCOME_EMPTY:
            missing_items.append("查询成功执行但结果为空（无记录，属取数缺项，不得补零）")

        if latest is not None and latest.get("outcome") == OUTCOME_HAS_ROWS:
            outcome = OUTCOME_HAS_ROWS
        elif latest is not None and latest.get("outcome") == OUTCOME_EMPTY:
            outcome = OUTCOME_EMPTY
        else:
            outcome = OUTCOME_FAILED

        return {
            "query_template_id": template_id,
            "query_template_name": query_row.get("模板名称", ""),
            "template_status": query_row.get("status", ""),
            "published": published,
            "dimensions": query_row.get("维度组合", ""),
            "filters": query_row.get("过滤条件", ""),
            "caliber_version": (latest.get("caliber_version") if latest else None)
            or (active_caliber.get("版本号") if active_caliber else None),
            "caliber_desc": active_caliber.get("口径描述") if active_caliber else "无已生效口径",
            "source": latest.get("source") if latest else "none",
            "outcome": outcome,
            "row_count": int(latest.get("row_count", 0)) if latest else 0,
            "sample_rows": list(latest.get("sample_rows", [])) if latest else [],
            "executed_at": latest.get("executed_at") if latest else None,
            "operator": latest.get("operator") if latest else None,
            "error": (latest.get("error", "") if latest else "") or execution_error,
            "missing_items": missing_items,
            "retriable": published and active_caliber is not None,
        }

    def _build_section(self, metric_name: str,
                       builder: QueryBuilderService,
                       operator: str, execute: bool) -> dict[str, Any]:
        """聚合一个指标（一章）下所有查询模板的取数覆盖。"""
        queries = store.find_by_field("query_builder", "关联指标", metric_name)
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)

        templates = [
            self._build_template_coverage(q, builder, operator, execute)
            for q in queries
        ]

        has_data = [t for t in templates if t["outcome"] == OUTCOME_HAS_ROWS]
        empty_ones = [t for t in templates if t["outcome"] == OUTCOME_EMPTY]
        failed_ones = [t for t in templates if t["outcome"] == OUTCOME_FAILED]

        if has_data:
            coverage = COVERAGE_READY
        elif empty_ones and not failed_ones:
            coverage = COVERAGE_EMPTY
        elif empty_ones:
            # 既有空结果又有失败模板：空结果证明链路可执行，但仍有缺项待补
            coverage = COVERAGE_EMPTY
        else:
            coverage = COVERAGE_BLOCKED

        # 章节级样本只汇总真实取到的数据；空/失败不产生任何零值行。
        section_samples: list[dict[str, Any]] = []
        for t in has_data:
            for row in t["sample_rows"]:
                if len(section_samples) >= SAMPLE_ROW_LIMIT:
                    break
                section_samples.append(dict(row))
        usable_row_count = sum(t["row_count"] for t in has_data)

        missing: list[str] = []
        if not queries:
            missing.append("指标未关联任何查询模板，无取数通道")
        if active_caliber is None:
            missing.append("指标没有已生效的口径版本")
        for t in templates:
            missing.extend(t["missing_items"])
        # 去重保持顺序
        missing = list(dict.fromkeys(missing))

        return {
            "metric": metric_name,
            "coverage": coverage,
            "caliber_version": active_caliber.get("版本号") if active_caliber else None,
            "caliber_desc": active_caliber.get("口径描述") if active_caliber else "无已生效口径",
            "query_template_count": len(templates),
            "usable_template_count": len(has_data),
            "empty_template_count": len(empty_ones),
            "blocked_template_count": len(failed_ones),
            "usable_row_count": usable_row_count,
            "sample_rows": section_samples,
            "templates": templates,
            "missing_items": missing,
            "retriable": any(t["retriable"] for t in templates),
            "chart_ready": coverage == COVERAGE_READY,
        }

    def build_coverage_view(self, entry_id: int, operator: str = "编制人",
                            execute: bool = False) -> dict[str, Any]:
        """生成报告取数覆盖视图：按指标集聚合章节样本、口径和取数缺项。"""
        template = store.find(self.MODULE, entry_id)
        if template is None:
            return {"error": f"报告模板 {entry_id} 不存在"}

        metric_set = template.get("包含指标集", "")
        metric_names = [m.strip() for m in metric_set.split(",") if m.strip()]
        builder = QueryBuilderService()

        sections = [
            self._build_section(name, builder, operator, execute)
            for name in metric_names
        ]

        ready = [s for s in sections if s["coverage"] == COVERAGE_READY]
        empty = [s for s in sections if s["coverage"] == COVERAGE_EMPTY]
        blocked = [s for s in sections if s["coverage"] == COVERAGE_BLOCKED]

        return {
            "template_id": entry_id,
            "report_template": template.get("模板名称"),
            "period": template.get("报告周期"),
            "operator": operator,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sections": sections,
            "summary": {
                "total_sections": len(sections),
                "ready_sections": len(ready),
                "empty_sections": len(empty),
                "blocked_sections": len(blocked),
                "chart_ready_sections": len(ready),
                # 可引用图表数 = 有真实数据的查询模板数；缺项不计零
                "usable_charts": sum(s["usable_template_count"] for s in sections),
            },
        }

    # ── 报告生成（章节级容错） ──

    def generate_report(self, entry_id: int, operator: str = "编制人") -> dict[str, Any] | None:
        """生成报告稿件：逐章独立取数，单章模板失败不影响其他章节。

        返回的稿件包含完整取数覆盖视图并持久化，可反复打开核对；
        空结果与无法执行严格区分，章节汇总只统计真实样本，绝不拿缺项补零。
        """
        template = store.find(self.MODULE, entry_id)
        if template is None:
            return {"error": f"报告模板 {entry_id} 不存在"}
        if template["status"] != "已发布":
            return {"error": f"报告模板 {entry_id} 未发布"}

        view = self.build_coverage_view(entry_id, operator=operator, execute=True)
        if "error" in view:
            return view

        sections = []
        for sec in view["sections"]:
            sections.append({
                "metric": sec["metric"],
                "coverage": sec["coverage"],
                "caliber_version": sec["caliber_version"],
                "caliber_desc": sec["caliber_desc"],
                "usable_row_count": sec["usable_row_count"],
                "sample_rows": sec["sample_rows"],
                "query_results_count": sec["usable_template_count"],
                "missing_items": sec["missing_items"],
                "chart_ready": sec["chart_ready"],
                "templates": sec["templates"],
            })

        draft = {
            "template_id": entry_id,
            "report_template": view["report_template"],
            "period": view["period"],
            "generated_at": view["generated_at"],
            "operator": operator,
            "format": template.get("输出格式", "HTML"),
            "sections": sections,
            "coverage_view": view,
            "total_metrics": len(sections),
        }
        stored = store.save_report_draft(draft)
        draft["draft_id"] = stored["id"]
        return draft

    def open_draft(self, draft_id: int) -> dict[str, Any] | None:
        """重新打开同一份稿件，查看当前各章节可引用情况（取数留痕为最新状态）。"""
        draft = store.get_report_draft(draft_id)
        if draft is None:
            return {"error": f"报告稿件 {draft_id} 不存在"}
        # 取数覆盖视图按最新执行留痕重算，稿件正文保留生成时快照。
        refreshed = self.build_coverage_view(
            int(draft["template_id"]), operator=draft.get("operator", "编制人"), execute=False)
        result = dict(draft)
        result["coverage_view"] = refreshed if "error" not in refreshed else draft.get("coverage_view")
        return result

    def list_drafts(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        """列出报告稿件（可按模板过滤），供编制人回到同一稿件继续核对。"""
        drafts = store.report_drafts(entry_id)
        return [
            {
                "draft_id": d["id"],
                "template_id": d.get("template_id"),
                "report_template": d.get("report_template"),
                "period": d.get("period"),
                "generated_at": d.get("generated_at"),
                "operator": d.get("operator"),
                "total_metrics": d.get("total_metrics"),
                "summary": d.get("coverage_view", {}).get("summary", {}),
            }
            for d in drafts
        ]

    def refetch_missing(self, entry_id: int, operator: str = "编制人",
                        draft_id: int | None = None) -> dict[str, Any]:
        """补取失败/缺项章节：只重新执行可执行（已发布且有生效口径）的查询模板。

        可选 draft_id：补取后刷新同一稿件的取数覆盖视图，便于重新打开核对。
        """
        template = store.find(self.MODULE, entry_id)
        if template is None:
            return {"error": f"报告模板 {entry_id} 不存在"}

        metric_set = template.get("包含指标集", "")
        metric_names = [m.strip() for m in metric_set.split(",") if m.strip()]
        builder = QueryBuilderService()

        refetch_results: list[dict[str, Any]] = []
        for metric_name in metric_names:
            for q in store.find_by_field("query_builder", "关联指标", metric_name):
                if q.get("status") != "已发布":
                    continue
                calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
                if not any(c.get("status") == "已生效" for c in calibers):
                    continue
                qid = int(q.get("id", 0))
                result = builder.execute_query(qid, operator=operator)
                latest = store.latest_query_run(qid)
                refetch_results.append({
                    "query_template_id": qid,
                    "query_template_name": q.get("模板名称", ""),
                    "metric": metric_name,
                    "outcome": latest.get("outcome") if latest else OUTCOME_FAILED,
                    "row_count": latest.get("row_count", 0) if latest else 0,
                    "caliber_version": latest.get("caliber_version") if latest else None,
                    "error": result.get("error", "") if isinstance(result, dict) and "error" in result else "",
                })

        view = self.build_coverage_view(entry_id, operator=operator, execute=False)

        refreshed_draft = None
        if draft_id is not None:
            draft = store.get_report_draft(draft_id)
            if draft is not None:
                draft["coverage_view"] = view
                # 同步刷新稿件内章节状态，保留生成时的正文快照字段
                for sec in draft.get("sections", []):
                    latest_sec = next(
                        (s for s in view.get("sections", []) if s["metric"] == sec["metric"]), None)
                    if latest_sec is not None:
                        sec["coverage"] = latest_sec["coverage"]
                        sec["usable_row_count"] = latest_sec["usable_row_count"]
                        sec["sample_rows"] = latest_sec["sample_rows"]
                        sec["missing_items"] = latest_sec["missing_items"]
                        sec["chart_ready"] = latest_sec["chart_ready"]
                        sec["templates"] = latest_sec["templates"]
                refreshed_draft = self.open_draft(draft_id)

        return {
            "template_id": entry_id,
            "refetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "operator": operator,
            "refetch_results": refetch_results,
            "coverage_view": view,
            "draft": refreshed_draft,
        }
