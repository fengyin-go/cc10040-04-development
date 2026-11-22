"""查询构建器领域服务：构建和缓存多维组合查询模板以加速分析。

消费口径版本的指标定义，生成查询模板供数据服务调用。
跨模块数据流: caliber_version -> query_builder -> data_service -> cache_layer -> query_journal

每次执行都会在 store 中留下「查询运行记录」：实际使用的样本、口径版本、
取数来源以及取数结果（成功有数据 / 成功无记录 / 执行失败），供报告取数视图核对。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService

# 取数结果三态：空结果与无法执行必须分清，汇总不得把缺项当零。
OUTCOME_HAS_ROWS = "成功有数据"
OUTCOME_EMPTY = "成功无记录"
OUTCOME_FAILED = "执行失败"

SAMPLE_ROW_LIMIT = 3


class QueryBuilderService(BaseModuleService):
    MODULE = "query_builder"
    ENTITY = "查询模板"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["模板名称", "关联指标", "维度组合", "过滤条件", "缓存策略"]
    REQUIRED_FIELDS = ["模板名称", "关联指标", "维度组合"]
    STATUS_ORDER = ["草稿", "已发布", "已过期"]
    ACTION_RULES = {"发布模板": "已发布", "过期模板": "已过期"}
    NEGATIVE_ACTIONS = []

    def _record_failure(self, template: dict[str, Any] | None, reason: str,
                        operator: str, metric_name: str = "") -> dict[str, Any]:
        """无法执行也要留痕：没有样本、没有口径，明确标注失败原因。"""
        run = {
            "template_id": int(template.get("id", 0)) if template else 0,
            "template_name": template.get("模板名称", "") if template else "",
            "metric": metric_name or (template.get("关联指标", "") if template else ""),
            "dimensions": template.get("维度组合", "") if template else "",
            "filters": template.get("过滤条件", "") if template else "",
            "caliber_version": None,
            "source": "none",
            "outcome": OUTCOME_FAILED,
            "row_count": 0,
            "sample_rows": [],
            "operator": operator,
            "error": reason,
        }
        if template is not None:
            store.record_query_run(int(template.get("id", 0)), run)
        return run

    def _record_success(self, template: dict[str, Any], caliber_version: str,
                        source: str, rows: list[dict[str, Any]], operator: str,
                        duration_ms: float = 0.0) -> dict[str, Any]:
        """成功执行留痕：空结果与有数据分开记录，样本行只留前几条供核对。"""
        outcome = OUTCOME_HAS_ROWS if rows else OUTCOME_EMPTY
        run = {
            "template_id": int(template.get("id", 0)),
            "template_name": template.get("模板名称", ""),
            "metric": template.get("关联指标", ""),
            "dimensions": template.get("维度组合", ""),
            "filters": template.get("过滤条件", ""),
            "caliber_version": caliber_version,
            "source": source,
            "outcome": outcome,
            "row_count": len(rows),
            "sample_rows": [dict(r) for r in rows[:SAMPLE_ROW_LIMIT]],
            "operator": operator,
            "duration_ms": round(duration_ms, 3),
            "error": "",
        }
        store.record_query_run(int(template.get("id", 0)), run)
        return run

    def execute_query(self, entry_id: int, operator: str = "系统",
                      use_cache: bool = True) -> dict[str, Any] | None:
        """执行查询模板并返回结果与日志。

        跨模块数据流: query_builder -> caliber_version(口径验证) -> cache_layer(缓存) -> query_journal(日志)
        无论成功（含空结果）还是失败，都会写入一条查询运行记录。
        """
        template = store.find(self.MODULE, entry_id)
        if template is None:
            self._record_failure(None, f"查询模板 {entry_id} 不存在", operator)
            return {"error": f"查询模板 {entry_id} 不存在"}
        if template["status"] != "已发布":
            run = self._record_failure(
                template, f"查询模板 {entry_id} 未发布（当前状态：{template.get('status')}）",
                operator)
            return {"error": run["error"]}

        metric_name = template.get("关联指标", "")
        if not metric_name:
            run = self._record_failure(template, "查询模板未关联指标", operator)
            return {"error": run["error"]}

        # 检查指标口径版本
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)
        if active_caliber is None:
            run = self._record_failure(
                template, f"指标 {metric_name} 没有已生效的口径版本，无法执行取数",
                operator, metric_name=metric_name)
            return {"error": run["error"]}

        caliber_version = active_caliber.get("版本号", "")
        cache_key = f"query:{metric_name}:{template.get('维度组合','')}:{template.get('过滤条件','')}"

        # 尝试读取缓存
        if use_cache:
            cached = store.cache_get(cache_key)
            if cached is not None:
                t_start = time.time()
                store.log_query(
                    metric_id=template.get("id", 0),
                    operator=operator,
                    sql_summary=f"缓存命中: {template.get('模板名称')}",
                    row_count=len(cached) if isinstance(cached, list) else 1,
                    duration_ms=(time.time() - t_start) * 1000,
                    status="正常",
                )
                run = self._record_success(
                    template, caliber_version, "cache",
                    cached if isinstance(cached, list) else [cached], operator)
                return {"source": "cache", "template": template.get("模板名称"),
                        "caliber": caliber_version, "rows": cached,
                        "outcome": run["outcome"]}

        # 模拟查询执行 - 基于指标数据生成结果
        t_start = time.time()
        metric_data = store.find_by_field("metric_definition", "指标名称", metric_name)
        dimensions = template.get("维度组合", "").split(",") if template.get("维度组合") else []

        result_rows = []
        for idx, m in enumerate(metric_data):
            row = {"序号": idx + 1, "指标": metric_name,
                   "口径版本": caliber_version,
                   "计算口径": m.get("计算口径", ""),
                   "统计粒度": m.get("统计粒度", "")}
            for dim in dimensions:
                row[dim.strip()] = f"维度值_{idx + 1}"
            result_rows.append(row)

        duration_ms = (time.time() - t_start) * 1000
        # 空结果是正常执行但无记录，不算慢查询也不算异常；日志返回行数如实写 0。
        status = "慢查询" if duration_ms > 100 and result_rows else "正常"

        # 写入查询日志
        store.log_query(
            metric_id=template.get("id", 0),
            operator=operator,
            sql_summary=f"查询模板: {template.get('模板名称')}, 维度: {template.get('维度组合')}",
            row_count=len(result_rows),
            duration_ms=duration_ms,
            status=status,
        )

        # 写入缓存（空结果同样缓存，避免反复穿透；报告取数视图按留痕区分空/缺）
        cache_ttl = 300  # 默认 5 分钟
        cache_policy = template.get("缓存策略", "")
        if "长期" in str(cache_policy):
            cache_ttl = 3600
        elif "短期" in str(cache_policy):
            cache_ttl = 60
        store.cache_set(cache_key, result_rows, cache_ttl)

        run = self._record_success(
            template, caliber_version, "computed", result_rows, operator, duration_ms)
        return {"source": "computed", "template": template.get("模板名称"),
                "caliber": caliber_version, "duration_ms": duration_ms,
                "cached_for_seconds": cache_ttl, "rows": result_rows,
                "outcome": run["outcome"]}

    def get_execution_runs(self, entry_id: int) -> list[dict[str, Any]]:
        """返回某查询模板的全部实际执行留痕，供取数覆盖视图核对样本与口径。"""
        return store.query_runs(entry_id)

    def invalidate_query_cache(self, metric_name: str) -> int:
        """当口径版本变更时，失效指标相关的所有查询缓存。

        跨模块数据流: caliber_version -> query_builder -> cache_layer
        """
        return store.cache_invalidate_by_prefix(f"query:{metric_name}:")
