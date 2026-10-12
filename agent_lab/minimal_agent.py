"""最小 Agent 教学示例：状态 + 工具 + while 循环。

这个文件不依赖模型服务，也不接触生产 RAG。它用一个可重复的规则决策器
模拟模型输出，专门展示 Agent Runtime 如何执行工具调用并把结果写回状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AgentState:
    user_goal: str
    history: list[dict[str, str]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    retries: dict[str, int] = field(default_factory=dict)
    step: int = 0


def calculator(expression: str) -> str:
    """教学用计算工具。只允许数字和基础算术符号。"""
    allowed = set("0123456789+-*/(). %")
    if not expression or any(char not in allowed for char in expression):
        return "计算失败：表达式包含不允许的字符"
    try:
        # 教学示例使用受限字符集；生产代码应使用专门的安全表达式解析器。
        value = eval(expression, {"__builtins__": {}}, {})  # noqa: S307
    except (ArithmeticError, SyntaxError, TypeError, ValueError):
        return "计算失败：表达式无法计算"
    return str(value)


def search_knowledge(query: str) -> str:
    """模拟知识库工具，展示 RAG 接入点。"""
    knowledge = {
        "血压": "家庭测量血压前应安静休息，并记录多次测量结果。",
        "血红蛋白": "血红蛋白结果必须结合报告参考范围和临床情况解释。",
    }
    for keyword, evidence in knowledge.items():
        if keyword in query:
            return f"证据[{keyword}]: {evidence}"
    return "证据不足：知识库没有找到直接支持当前问题的内容。"


def flaky_search(query: str) -> str:
    """示范工具失败；运行时会在第一次失败后重试。"""
    return "工具错误：知识库连接暂时失败，请重试"


TOOLS: dict[str, Callable[..., str]] = {
    "calculator": calculator,
    "search_knowledge": search_knowledge,
    "flaky_search": flaky_search,
}


def decide_next_action(state: AgentState) -> dict[str, Any]:
    """模拟模型决策：真实项目中这里替换成 LLM 的 tool call 输出。"""
    if not state.tool_results:
        if "不稳定" in state.user_goal:
            return {
                "type": "tool_call",
                "name": "flaky_search",
                "arguments": {"query": state.user_goal},
            }
        if any(char.isdigit() for char in state.user_goal) and any(
            operator in state.user_goal for operator in "+-*/"
        ):
            return {
                "type": "tool_call",
                "name": "calculator",
                "arguments": {"expression": state.user_goal},
            }
        return {
            "type": "tool_call",
            "name": "search_knowledge",
            "arguments": {"query": state.user_goal},
        }

    result = state.tool_results[-1]["result"]
    if result.startswith("工具错误"):
        tool_name = state.tool_results[-1]["tool"]
        if state.retries.get(tool_name, 0) <= 1:
            return {
                "type": "tool_call",
                "name": tool_name,
                "arguments": state.tool_results[-1]["arguments"],
            }
        return {"type": "refuse", "content": "工具连续失败，暂时无法完成任务。"}
    if result.startswith("证据["):
        return {
            "type": "final_answer",
            "content": f"根据检索到的证据：{result}",
        }
    return {"type": "final_answer", "content": f"工具结果：{result}"}


def run_agent(user_goal: str, *, max_steps: int = 4) -> str:
    state = AgentState(user_goal=user_goal)

    while state.step < max_steps:
        state.step += 1
        decision = decide_next_action(state)
        state.history.append({"role": "agent", "content": str(decision)})
        print(f"[第 {state.step} 步] 决策: {decision}")

        if decision["type"] == "tool_call":
            tool_name = decision["name"]
            tool = TOOLS.get(tool_name)
            if tool is None:
                return f"安全停止：未知工具 {tool_name}"
            result = tool(**decision["arguments"])
            if result.startswith("工具错误"):
                state.retries[tool_name] = state.retries.get(tool_name, 0) + 1
            state.tool_results.append(
                {"tool": tool_name, "arguments": decision["arguments"], "result": result}
            )
            state.history.append({"role": "tool", "content": result})
            print(f"[第 {state.step} 步] 工具结果: {result}")
            continue

        if decision["type"] == "final_answer":
            return decision["content"]

        if decision["type"] == "refuse":
            return decision["content"]

        return "安全停止：无法识别的决策类型"

    return "安全停止：超过最大执行步数"


if __name__ == "__main__":
    print("--- 例 1：工具调用 + 最终回答 ---")
    print(run_agent("血压应该怎么测量？"))
    print()
    print("--- 例 2：另一个工具 ---")
    print(run_agent("12 * (3 + 4)"))
    print()
    print("--- 例 3：工具失败后的重试与安全停止 ---")
    print(run_agent("模拟一次不稳定的知识库查询"))
