# 医疗 RAG 检索评测报告（2026-10-13）

- 冻结集：120 条；SHA-256：`ed33014ce05d20b8cf98eb915833183a8e9284691eb996d71be89b9a90c20e5b`
- 索引：`medical-rag-public-local` / `medical-rag-private-local`
- 模型：`BAAI/bge-small-zh-v1.5`，维度 512

## 检索与拒答

- 配置：`G1-hybrid-rrf60-gate`
- Recall@3：0.7571428571428571
- Precision@3：0.30476190476190473
- MRR：0.6642857142857143
- nDCG@3：0.6894940492353537
- 拒答准确率：0.8
- 安全全零：True
- answerable 误拒：10 条；拒答误接纳：6 条

## 引用

- 标注陈述数：30
- Citation validity：1.0
- Citation accuracy：1.0
- Citation coverage：1.0

## 知识缺口

- 已确认缺口：0

阈值只使用 dev 集选择；冻结 test 集仅用于本次最终确认。
