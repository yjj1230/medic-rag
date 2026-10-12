# RAG Test 集

文件：`rag_cases.test.jsonl`

这是最终验收集，不用于调参。当前包含 30 条问题：17 条质量、10 条拒答、3 条安全。

## 使用规则

1. 调整 `top_k`、RRF、阈值、reranker 或 prompt 时只使用 dev 集。
2. Test 集标注冻结后，不根据单次结果修改 gold；如果发现标注错误，要记录新的 revision，并重新冻结。
3. `gold_evidence_ids` 只放能直接支持答案的证据；只有主题相关、但不能支撑具体结论的 chunk 不算 gold。
4. 拒答问题要配合拒答阈值或分流实验评估，不能只看普通检索是否返回空。
5. 安全用例采用零容忍规则：`forbidden_evidence_ids` 中任一证据被返回，都算失败。

运行快速 smoke test：

```powershell
python scripts/retrieval_tuning_lab.py `
  --cases-file eval/rag_cases.test.jsonl `
  --quick --rounds 5 --out eval_out/test_run
```

当前 test 集的来源快照为 public 18 条、private 2 条；私有用例使用已有的合成 `blood_report.pdf` 记录，不新增患者数据。private 中相同 `content_hash` 只保留最新 canonical evidence，黑方块占位符记录已标记为不可检索并从索引移除；原始 PDF 仍保留在对象存储中。


## 拒答阈值验证

`0.52` 是在 Dev 阶段预先选定的候选阈值；Test 集只用于确认，不用于重新挑选阈值。验证结果见 `eval_out/test_rejection/rejection_threshold_sweep.json`。private 去重后的最新检索 smoke 结果见 `eval_out/test_run_after_private_cleanup_v2/retrieval_eval_results.json`。
