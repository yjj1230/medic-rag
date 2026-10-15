"""文档类型黄金集评测与发布门禁（Task 10）。

本包托管唯一一个可复用的管线运行器，使 pytest 评测
（``tests/eval/test_document_quality.py``）与 CLI 发布门禁命令
（``python -m medical_rag.cli evaluate-document-quality``）共享完全相同的
parse→clean→chunk→extract→gate 调用。这里不触碰任何生产索引：评测完全在内存中
基于合成黄金 fixture 进行。
"""
