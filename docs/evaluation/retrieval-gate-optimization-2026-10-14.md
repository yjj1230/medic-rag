# 医疗 RAG 检索评测报告（2026-10-13）

- 冻结集：120 条；SHA-256：`ed33014ce05d20b8cf98eb915833183a8e9284691eb996d71be89b9a90c20e5b`
- 索引：`medical-rag-public-local` / `medical-rag-private-local`
- 模型：`BAAI/bge-small-zh-v1.5`，维度 512
- 分数空间：映射 `cosinesimil`；记录 `cosinesimil`
- 门禁阈值：cosine=0.76；vector-only=0.8

## 检索与拒答

- 配置：`G1-hybrid-rrf60-gate`
- pre Recall@3：0.9428571428571428
- pre Precision@3：0.32857142857142857
- pre MRR：0.8119047619047619
- pre nDCG@3：0.846975749439437
- pre 拒答准确率：0.0
- pre 误拒率：0.0
- pre 安全：{'n': 20, 'permission_violations': 0, 'deletion_residuals': 0, 'review_leakage': 0, 'all_zero': True}
- pre 延迟：p50=28.02249975502491 ms，p95=41.5154998190701 ms，p99=43.8701999373734 ms
- post Recall@3：0.8714285714285714
- post Precision@3：0.3619047619047619
- post MRR：0.7476190476190476
- post nDCG@3：0.7808196101027018
- post 拒答准确率：0.6333333333333333
- post 误拒率：0.07142857142857142
- post 安全：{'n': 20, 'permission_violations': 0, 'deletion_residuals': 0, 'review_leakage': 0, 'all_zero': True}
- post 延迟：p50=27.00390014797449 ms，p95=45.37760000675917 ms，p99=48.70630009099841 ms
- answerable 误拒：5 条；拒答误接纳：11 条
- pre/post：A3-hybrid-rrf60 → G1-hybrid-rrf60-gate
- 延迟：p50=27.00390014797449 ms，p95=45.37760000675917 ms，p99=48.70630009099841 ms

## 引用

- 标注陈述数：30
- Citation validity：1.0
- Citation accuracy：1.0
- Citation coverage：1.0

## 失败归因

- 标签计数（分母 120）：{'retrieval_miss': 4, 'gate_false_reject': 5, 'gate_false_accept': 11, 'annotation_issue': 0, 'insufficient_corpus': 0, 'permission_or_lifecycle': 0, 'infrastructure_failure': 0, 'manual_review': 100, 'total': 120}
- 未决人工复核 ID：['who-definition', 'who-two-days', 'who-silent', 'who-numbers', 'who-organ-risk', 'who-salt', 'who-tobacco', 'who-goal', 'who-prevention', 'medlineplus-nav', 'dash-core', 'dash-sodium', 'dash-lower', 'dash-dairy', 'dash-fat', 'lifestyle-exercise', 'lifestyle-salt', 'lifestyle-weight', 'lifestyle-drugs', 'preg-types', 'preg-20weeks', 'preg-preeclampsia', 'preg-postpartum', 'preg-warning-headache', 'preg-monitor', 'preg-medication', 'preg-hospital', 'preg-single-reading', 'preg-adult-mix', 'measure-rest', 'measure-feet', 'measure-arm', 'measure-repeat', 'measure-factors', 'measure-record', 'measure-home-target', 'measure-device-use', 'meds-stop', 'meds-missed', 'meds-discomfort', 'treat-plan', 'emergency-breath', 'emergency-no-symptom', 'symptom-diagnosis', 'who-sodium', 'preg-fetal', 'preg-postpartum-care', 'medlineplus-diagnosis', 'causes-meds', 'risk-check', 'crisis-action', 'priv-rbc-unit', 'priv-platelet-compare', 'priv-hgb-low', 'priv-wbc-value', 'priv-report-followup', 'priv-cbc-items', 'priv-platelet-unit', 'priv-hgb-reference', 'priv-rbc-reference', 'priv-advice-safety', 'rej-thyroid', 'rej-asthma', 'rej-fracture', 'rej-cad', 'rej-copd', 'rej-gout', 'rej-pneumonia', 'rej-lipid', 'rej-anticoag', 'rej-ct', 'rej-liver', 'rej-lab-diagnosis', 'rej-prompt', 'rej-code', 'rej-mixed', 'rej-malformed', 'rej-all-patients', 'rej-ocr', 'rej-secret', 'sec-v2-patient-b-1', 'sec-v2-patient-b-2', 'sec-v2-patient-b-3', 'sec-v2-patient-b-4', 'sec-v2-patient-b-5', 'sec-v2-patient-b-6', 'sec-v2-patient-b-7', 'sec-v2-patient-b-8', 'sec-v2-patient-b-9', 'sec-v2-patient-b-10', 'sec-v2-tenant-b-1', 'sec-v2-tenant-b-2', 'sec-v2-tenant-b-3', 'sec-v2-tenant-b-4', 'sec-v2-tenant-b-5', 'sec-v2-tenant-b-6', 'sec-v2-tenant-b-7', 'sec-v2-tenant-b-8', 'sec-v2-tenant-b-9', 'sec-v2-tenant-b-10']

## 知识缺口

- 已确认缺口：0

阈值只使用 dev 集选择；冻结 test 集仅用于本次最终确认。
