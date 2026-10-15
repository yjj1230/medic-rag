from __future__ import annotations

import pytest

from medical_rag.query.decomposer import QueryDecomposer


def _texts(question: str) -> tuple[str, ...]:
    return tuple(part.text for part in QueryDecomposer().decompose(question))


def test_decomposer_splits_independent_questions_in_original_order() -> None:
    assert _texts("我的血红蛋白是多少？白细胞正常吗？") == (
        "我的血红蛋白是多少？",
        "白细胞正常吗？",
    )


def test_decomposer_splits_numbered_questions() -> None:
    assert _texts("1. 我的血红蛋白是多少？2. 白细胞正常吗？") == (
        "我的血红蛋白是多少？",
        "白细胞正常吗？",
    )


def test_decomposer_splits_connector_after_first_question() -> None:
    assert _texts("我的血红蛋白是多少？另外高血压平时要注意什么？") == (
        "我的血红蛋白是多少？",
        "高血压平时要注意什么？",
    )


def test_decomposer_keeps_shared_normal_range_request_as_one_part() -> None:
    assert _texts("血压和血糖的正常范围分别是多少？") == (
        "血压和血糖的正常范围分别是多少？",
    )


def test_decomposer_removes_empty_numbered_items() -> None:
    assert _texts("1. 我的血红蛋白是多少？ 2.  3. 白细胞正常吗？") == (
        "我的血红蛋白是多少？",
        "白细胞正常吗？",
    )


def test_decomposer_rejects_more_than_six_parts() -> None:
    question = "？".join(f"第{i}项是什么" for i in range(1, 8)) + "？"

    with pytest.raises(ValueError, match="最多支持 6 个子问题"):
        QueryDecomposer().decompose(question)

