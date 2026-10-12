"""Document-type golden-set evaluation and release-gating (Task 10).

This package hosts ONE reusable pipeline runner so the pytest evaluation
(``tests/eval/test_document_quality.py``) and the CLI release-gate command
(``python -m medical_rag.cli evaluate-document-quality``) share the exact same
parse→clean→chunk→extract→gate invocation.  Nothing here touches a production
index: evaluation is fully in-memory over the synthetic golden fixtures.
"""
