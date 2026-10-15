from __future__ import annotations

import pytest

from medical_rag.query.router import QueryRoute, QueryRouter


@pytest.mark.parametrize(
    ("question", "expected_route"),
    [
        ("我的血红蛋白是多少？", QueryRoute.SINGLE_QUERY),
        ("血压和血糖的正常范围是什么？", QueryRoute.SINGLE_QUERY),
        ("我的血红蛋白是多少？白细胞正常吗？", QueryRoute.MULTI_QUERY),
        (
            "1. 我的血红蛋白是多少？2. 白细胞正常吗？",
            QueryRoute.MULTI_QUERY,
        ),
        (
            "我的血红蛋白是多少？根据这个结果需要复查吗？",
            QueryRoute.MULTI_HOP,
        ),
        ("我能不能自己停药？", QueryRoute.SAFE_BLOCK),
    ],
)
def test_router_assigns_boundary_route(question: str, expected_route: QueryRoute) -> None:
    decision = QueryRouter().route(question)

    assert decision.route is expected_route
    assert 0.0 <= decision.confidence <= 1.0


def test_router_does_not_split_a_shared_normal_range_request() -> None:
    decision = QueryRouter().route("血压和血糖的正常范围分别是多少？")

    assert decision.route is QueryRoute.SINGLE_QUERY


def test_router_keeps_independent_questions_as_multi_query() -> None:
    decision = QueryRouter().route("我的血红蛋白是多少？另外高血压平时要注意什么？")

    assert decision.route is QueryRoute.MULTI_QUERY

