# 文档质量与知识库索引流水线设计

## 1. 背景

当前项目已经具备解析、EvidenceUnit、MedicalFact、Embedding、IndexJob、公共/私有索引和混合检索能力，但文档清洗、复杂版面恢复、语义分块和解析质量门禁仍不够完整。

文档进入索引前的质量会直接影响 BM25 召回、向量召回、RRF 排序、医学事实抽取和引用定位。本设计建立一条“原文可追溯、清洗可解释、分块可评估、事实可复核、索引可重建”的文档质量流水线。

## 2. 目标与非目标

### 目标

1. 增加独立的文档清洗模块，不让 Parser 同时承担清洗职责。
2. 统一 PDF、Word、Excel、图片的版面解析输出。
3. 将字符切分升级为标题、段落、句子、表格感知的语义分块。
4. 扩展结构化医学事实模型，保留原始值、规范化值和来源锚点。
5. 在 Embedding 和索引写入前增加质量门禁。
6. 支持 parser、cleaner、chunker、extractor 版本化和可重复重建。
7. 保持公共知识与患者私有证据的索引和权限路径分离。

### 非目标

- P0 不引入自主 Agent、查询改写或会话记忆。
- 不用 LLM 改写或替换原始医学文本。
- 不把低置信度结果直接当作高可信医学事实。
- 不在本设计中实现完整人工审核前端。

## 3. 总体架构

```text
【患者私有文档链】
原始文件
  → FileTypeDetector
  → Parser（PDF/Word/Excel/OCR，版面恢复）
  → DocumentCleaner
  → LayoutNormalizer
  → SemanticChunker
  → EvidenceBuilder
  → MedicalFactExtractor
  → QualityGate
  → IndexJob / Outbox
  → EmbeddingPipeline
  → PrivateIndexWriter
  → OpenSearch（medical-rag-private）

【公共知识链】
知识源登记（机构/标题/版本/发布日期/审核人）
  → 已结构化知识文本（markdown/结构化指南）
  → TextSourceParser（包装为 ParsedBlock 流，跳过版面恢复与 OCR）
  → DocumentCleaner
  → SemanticChunker
  → QualityGate
  → IndexJob / Outbox
  → EmbeddingPipeline
  → PublicIndexWriter
  → OpenSearch（medical-rag-public）
```

两条链共用 `ParsedBlock` / `CleanedDocument` / `QualityGate` 的契约与实现，分叉点只在 Parser：
患者文档走全量解析（版面恢复、逐页 OCR）；公共知识源默认是已结构化文本，
由 `TextSourceParser` 直接包装为块流，不做 OCR 与双栏恢复。

每个模块通过小而稳定的 seam 协作。复杂性集中在模块实现内部，调用方只依赖结构化结果和明确的失败状态。

## 4. 核心数据契约

### 4.1 ParsedBlock

Parser 不直接输出最终索引文本，而是输出版面块：

```python
ParsedBlock(
    block_id: str,
    block_type: Literal["heading", "paragraph", "list", "table", "image"],
    raw_text: str,
    page_number: int | None,
    sheet_name: str | None,
    cell_range: str | None,
    bbox: tuple[float, float, float, float] | None,
    section_path: tuple[str, ...],
    reading_order: int,
    language: str,
    confidence: float,
)
```

### 4.2 CleanedDocument

```python
CleanedDocument(
    raw_blocks: tuple[ParsedBlock, ...],
    cleaned_blocks: tuple[ParsedBlock, ...],
    source_offsets: tuple[SourceOffset, ...],
    cleaning_flags: tuple[str, ...],
    cleaner_version: str,
)
```

清洗结果必须保留原始文本和原始位置映射，禁止只覆盖原文。

### 4.3 EvidenceUnit

EvidenceUnit 至少包含：

```text
document_id
document_version_id
evidence_unit_id
kind
raw_text
clean_text
section_path
page_number / sheet_name / cell_range / bbox
source_anchor
chunk_index
parent_block_id
parser_version
cleaner_version
chunker_version
confidence
quality_flags
retrieval_eligible
```

### 4.4 MedicalFact

```text
canonical_name
raw_name
value_number
value_text
unit_raw
unit_normalized
reference_low
reference_high
abnormal_flag
collection_time
evidence_unit_id
source_anchor
confidence
review_status
```

原始值和规范化值必须同时保留，例如 `13.2 g/dL` 与 `132 g/L` 不能只保存其中一个。

## 5. 文档清洗设计

新增深模块：

```python
DocumentCleaner.clean(parsed_document) -> CleanedDocument
```

### 清洗规则

- Unicode、全角半角、特殊空格统一
- 去除不可见字符和明显乱码
- 页眉、页脚、页码识别与标记
- 重复段落、模板噪声去重
- OCR 断行、断词和异常符号修复
- 日期、数字和单位格式规范化
- 表格分隔符、空单元格和换行统一
- 记录每次清洗产生的 flag

### 清洗原则

1. 不改变医学事实的语义。
2. 不删除无法确认的内容，只标记异常。
3. 原文、清洗文本、规范化文本分层保存。
4. 清洗模块可独立回放和测试。

## 6. 解析与版面恢复

### PDF

- 按页判断是否需要 OCR，而不是只判断整份文档。
- 保留页面、坐标、阅读顺序和表格关系。
- 对双栏、页眉页脚、检验单布局增加专门处理。

### Word

- 保留标题层级、列表层级、段落顺序。
- 保留表格标题、表格位置和图片说明。

### Excel

- 识别 Sheet、表格区域和多级表头。
- 保留合并单元格、单元格类型、日期/数值类型和原始格式。
- 保留 `sheet_name`、`cell_range`、行列范围。

### 图片/OCR

- 保留 OCR 坐标和置信度。
- 对低置信度区域单独标记。
- 支持文本和扫描图混合页面。

## 7. 语义分块设计

新增或升级：

```python
SemanticChunker.chunk(cleaned_document) -> list[EvidenceUnitDraft]
```

### 普通文本

- 默认 300～600 tokens（以与 embedding 模型一致的 tokenizer 计数；工程实现以字符为快速近似，中文按 1 token ≈ 1.5 字符折算，即初始 450～900 字符，黄金集阶段校准）。
- 10%～15% overlap。
- 优先按标题、段落和句子边界切分。
- 保留 `section_path`、页面和来源锚点。
- 长段落不能简单按字符硬切，除非已记录明确的切分关系。

### 表格

- 表格标题和表头必须随每组数据重复。
- 每 10～30 行组成一个 chunk，具体值由评估集校准。
- 保留起止行和 `cell_range`。
- 关键检验行可以额外生成独立 fact chunk。

### 父子块

复杂文档采用：

```text
Parent block：完整章节或表格
Child chunk：用于向量和 BM25 检索的较小块
```

检索返回 child，引用和上下文扩展可以回到 parent。

### 父子块检索契约

- `SearchRecord` / `EvidenceUnit` 增加 `parent_block_id`；child 命中后可按它回查同 parent 的兄弟 chunk。
- 引用展示始终使用 child 自身的页码 / Sheet / Cell / 坐标；parent 只用于上下文扩展，不作为引用来源。
- 上下文扩展预算：最多回查 2 个 parent；每个 parent 最多取 4 个兄弟 chunk（按阅读顺序、去重）；扩展总量不超过模型上下文预算的固定比例（默认 30%，可配置）。
- 扩展块与命中块在提示词中以同一证据格式给出，并共享命中块的 evidence id 前缀（如 `e1` 与 `e1c2`），确保引用仍可定位到原始 child。

## 8. 结构化证据与复核

`MedicalFactExtractor` 负责抽取结构化事实，但不能替代原始证据。

```text
高置信度事实 → 可进入事实索引
低置信度事实 → 保存并 NEEDS_REVIEW
冲突事实     → 保存原文，阻止进入高可信事实索引
无法抽取     → 原始 Evidence 仍可检索
```

第一阶段优先覆盖：检验指标、诊断和疾病、药品/剂量/频次、症状体征、影像结论。

医学术语、单位和指标名采用可配置标准化字典，并保留原始名称。

## 9. 质量门禁

新增：

```python
QualityGate.evaluate(document, evidence, facts) -> QualityDecision
```

### 初始质量指标

- 文本覆盖率
- 非空块比例
- source_anchor 完整率
- OCR 平均置信度
- 异常字符比例
- 重复块比例
- 表格列数一致性
- 事实抽取覆盖率
- 页码、Sheet、Cell 定位准确率

### 决策

```text
PASS          → 创建/执行 IndexJob
NEEDS_REVIEW  → 保存结果，但 retrieval_eligible=False
FAIL          → 不创建可检索索引任务，等待重解析或人工处理
```

阈值先在黄金测试集上确定，不在代码中散落 magic number。

### 阈值冷启动

黄金测试集由合成与脱敏文档构成（PDF 文本报告、扫描 PDF、DOCX、XLSX、图片 OCR 各 5～10 份），
在真实患者文档积累前先用保守初始区间运行；前 50 份真实文档经人工复核后校准阈值，再逐步收紧。
患者文档不得未经脱敏直接进入黄金集。

### 不合格文档的运营出口（试点期）

人工复核工作台在 P1 才交付，因此 P0.5 必须先提供最小出口，避免 NEEDS_REVIEW / FAIL 文档静默堆积：

- 文档列表暴露解析质量状态（READY / NEEDS_REVIEW / FAILED_PARSE）与失败原因码。
- 上传者对 FAIL 文档可见原因并可直接重新上传；NEEDS_REVIEW 文档保留在库但不进入检索。

## 10. 索引与版本

索引记录必须携带：

```text
parser_version
cleaner_version
chunker_version
extractor_version
embedding_model
embedding_dimensions
index_version
```

任何清洗、分块、事实抽取或 Embedding 版本变化，都应产生新的 IndexJob 幂等键，并支持按文档版本重建。

数据库业务数据与 `IndexJob/OutboxEvent` 在同一个事务中提交；外部 OpenSearch 写入由 Worker 异步完成，失败可重试，写入必须幂等。

## 11. 错误和降级

| 阶段 | 失败处理 |
|---|---|
| 文件识别 | 拒绝并记录明确错误 |
| 解析 | 标记 ParseJob 失败，可重试 |
| 清洗 | 保留原文，进入 NEEDS_REVIEW |
| 分块 | 不生成不完整的可检索块 |
| 事实抽取 | 原始 Evidence 保留，事实进入复核 |
| 质量门禁 | 不通过则 `retrieval_eligible=False` |
| Embedding | IndexJob 失败并重试 |
| OpenSearch | 记录失败层、错误码和 attempts |
| 删除 | 不宣称完成，返回可重试层 |

## 12. 评估方案

建立按文档类型分层的黄金集：PDF 文本报告、扫描 PDF、DOCX、XLSX、图片 OCR、中英文混合、双栏和复杂表格。

### 解析指标

- 解析成功率
- 文本覆盖率
- 表格还原准确率
- source_anchor 准确率
- OCR 置信度
- 事实抽取 Recall/Precision

### 检索指标

- Chunk Recall@K
- Recall@K
- MRR
- 引用准确率
- 权限违规率
- 删除残留率
- P95 延迟

任何生产 alias 切换前必须满足：质量指标不低于基线，权限违规率和删除残留率为 0。

## 13. 实施阶段

### P0.5：质量基础设施（按 a/b/c 三片交付，每片可独立验收）

**P0.5a 契约与清洗**

1. 统一 `ParsedBlock` 和来源位置契约（页码、Sheet、Cell、bbox、section_path）。
2. 增加 `DocumentCleaner` 和 `CleanedDocument`（原文/清洗文本/来源偏移分层保留）。
3. 增加 `TextSourceParser`，公共知识源接入同一清洗契约。

**P0.5b 语义分块升级**

4. 将分块从字符策略升级为标题/段落/句子感知，支持 overlap 与表格表头重复。
5. 落地父子块（Parent/Child）与检索侧契约。
6. 核对现有 `ExcelParser` 与多级表头/合并单元格目标的差距，形成 P1 补齐清单。

**P0.5c 元数据、门禁与出口**

7. 扩展 EvidenceUnit 和 MedicalFact 元数据（含 parent_block_id、质量标记）。
8. 增加 QualityGate 与黄金测试集（合成/脱敏文档）。
9. 文档列表暴露解析质量状态与失败原因码（最小运营出口）。

### P1：复杂文档能力

1. PDF 版面恢复和逐页 OCR。
2. Excel 多级表头、类型和合并单元格处理（按 P0.5b 核对出的差距清单执行）。
3. 医学术语和单位标准化。
4. 复杂文档 Vision/LLM 兜底（外发调用必须复用 ModelGateway：EXTERNAL_MODEL 同意 + 零留存 + 审计）。
5. 人工复核工作台。

### P2：平台化能力

1. 真实 OpenSearch Writer 和固定 mapping。
2. 索引重建、对账和 alias 发布流程。
3. 解析质量监控和漂移检测。
4. 大规模批量索引和成本优化。

## 14. 验收标准

1. 普通 PDF、DOCX、XLSX、OCR 文档均能输出统一 `ParsedBlock`。
2. 所有可检索 chunk 都能回溯到页码、Sheet、Cell 或坐标。
3. 表格 chunk 不丢表头、不拆散关键行。
4. 待复核证据不会进入高可信检索结果。
5. 清洗、分块、事实抽取和 Embedding 版本可追踪。
6. 解析失败、索引失败和删除失败均可重试和观测。
7. 真实黄金集上的 Recall、MRR、引用准确率不低于当前基线。
8. 权限违规率和删除残留率为 0。
9. NEEDS_REVIEW / FAIL 文档在文档列表对上传者可见，并附失败原因码。
10. 公共知识链与患者文档链产出的块均通过同一 `ParsedBlock` 契约校验。
