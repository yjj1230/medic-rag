"""承重事实验证：现有安全/意图分类器在真实代码下的实际行为。

用途：为非医学分流 spec（2026-10-06-chat-nonmedical-routing-design.md）提供
「写 spec 前先验证承重事实」的证据。不写生产代码，只调用既有实现。

运行：
  C:\\Users\\lyx\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe \
      docs/superpowers/notes/evidence/2026-10-06-casual-routing-fact-probe.py
"""
import sys

sys.path.insert(0, r"C:\Users\lyx\Desktop\RAG\src")

from medical_rag.query.intent import IntentRouter
from medical_rag.query.intent_rules import score_intent
from medical_rag.safety.service import SafetyService
from medical_rag.safety.utterance import is_general_knowledge_enquiry

safety = SafetyService()
router = IntentRouter()

CASES = [
    "你好",
    "在吗",
    "今天天气怎么样",
    "谢谢",
    "1+1等于几",
    "给我讲个笑话",
    "自杀",
    "网上说自杀怎么回事",
    "我想自杀",
    "网上看到有人说胸痛怎么回事",
    "胸痛",
    "我停药了可以吗",
    "帮我看看我的报告",
    "高血压怎么预防",
    "血红蛋白是什么",
]

print("=" * 96)
print(f"{'question':<26} {'safety_action':<20} {'risk':<18} {'gke':<6} {'intent':<20} conf")
print("=" * 96)
for q in CASES:
    d = safety.classify(q)
    gke = is_general_knowledge_enquiry(q)
    intent, conf = router.classify(q)
    print(
        f"{q:<26} {d.action.value:<20} {d.risk_class.value:<18} "
        f"{str(gke):<6} {intent.value:<20} {conf}"
    )
    ranked = score_intent(q)
    if ranked:
        print(f"{'':26} rule_hits: {[(i.value, c) for i, c in ranked]}")

print()
print("=" * 96)
print("KEY: is the urgent word '自杀' exempted by is_general_knowledge_enquiry?")
for q in ["自杀", "网上说自杀怎么回事", "我想自杀", "有人说自杀"]:
    d = safety.classify(q)
    print(f"  {q!r:<24} action={d.action.value:<20} gke={is_general_knowledge_enquiry(q)}")
