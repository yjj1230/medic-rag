# ExcelParser P1 Gap Checklist（Task 3 Step 5 审计记录）

> 本文件是 **P1.2 积压输入**，不是本计划（Task 3）的工作内容。Task 3 只保证
> `ExcelParser` 产出增量 `blocks`（保留 sheet_name / cell_range / 精确 source_anchor），
> 不承诺以下设计目标。以下每项都附**具体失败用例**，供 P1.2 作为验收起点。

审计对象版本：`adapters.py::ExcelParser`（`spreadsheet-parser-v2`），当前用
`openpyxl.load_workbook(read_only=True, data_only=True)` / `xlrd` 读取，
`iter_rows(values_only=True)` 取单元格**数据值**并以 `str()` 落为文本。

---

## 结论（TL;DR）

ExcelParser 在 Task 3 里完成了**版面块的增量产出**：每张非空 sheet 产出一个
`ParsedBlock(block_type="table")`，带 `sheet_name`、`cell_range`、`source_anchor`
（`sheet:{sheet}!{range}`）与 confidence=1.0；legacy `segments`/`tables` 原样保留。
但相对 P1 设计目标（多级表头 / 合并单元格语义 / 键入日期数字）存在 5 项缺口，
全部推迟到 P1.2。

---

## Gap 1 — 键入日期/数字原始文本未保留（data_only 丢失格式）

**目标**：`ParsedBlock.raw_text` / `ParsedTable` 应保留单元格在电子表格上的**原始显示
文本**（如日期 `2024-01-02` 显示为 `2024/1/2`、数字保留千分位/小数位数）。

**现状**：`read_only=True, data_only=True` 只回传计算后的**值**；日期变成一个
`datetime` 对象然后 `str()` ⇒ `2024-01-02 00:00:00`，数字 `105` ⇒ `"105"`（丢失
`#,##0.00` 等格式），百分比/货币单位丢失。

**失败用例**（P1.2 应转绿的验收断言）：

```python
def test_excel_preserves_typed_date_and_number_display_text(workbook_bytes):
    result = ExcelParser().parse(workbook_bytes, ".xlsx")
    block = result.blocks[0]
    # 单元格 number_format='yyyy/m/d' 的日期应显示 '2024/1/2'，而非 datetime 的 str。
    assert "2024/1/2" in block.raw_text
    # 单元格 number_format='#,##0.00' 的数字应显示 '1,234.50'。
    assert "1,234.50" in block.raw_text
```

**影响**：清洗/检索喂入的文本不是用户在 Excel 里看到的原文 ⇒ 医疗数值与日期证据
可能失真（例如 `105` 与 `105.0`、日期 `01/02` 与 `2024/1/2`）。

**P1.2 方向**：改用非 `read_only` 加载并读取 `cell.number_format`；日期按格式渲染；
数字保留原格式显示文本。`.xls` 的 `xlrd` 同理需 `datemode` + 单元格格式。

---

## Gap 2 — 多级表头未被识别（首行即表头）

**目标**：识别多层级表头（如“检验项目 / 全血计数 / 血红蛋白  |  结果 / 参考范围”），
把表头层级与数据区正确分层，产出结构化 header 语义。

**现状**：`parsed_table.headers = normalized_rows[0]` 无条件把**首非空行**当表头，
不存在多级分组概念。

**失败用例**（P1.2）：

```python
def test_excel_detects_multi_level_headers(workbook_bytes):
    # 前两行是两级表头，第三行起是数据
    result = ExcelParser().parse(workbook_bytes, ".xlsx")
    table = result.tables[0]
    # 期望能区分 "全血计数"(父) + "血红蛋白"(子)；当前只会把第一行祖父当 headers。
    assert table.headers_grouped is not None  # P1.2 新增字段
```

**影响**：多级表头文档的表头行被当数据/表头串行，跨列语义丢失。

**P1.2 方向**：基于样式/边框/列重复检测表头行数；把 header 层级折叠进块元数据。

---

## Gap 3 — 合并单元格语义未保留

**目标**：合并单元格（如标题跨列合并、表头单元格合并）应保留下数据值及其跨越范围
（`rowspan`/`colspan`），避免合并只留左上角值、其余为空洞。

**现状**：`read_only` + `values_only=True` 把合并单元格折叠为**左上角值**、其余位置为
`None`/空元；未记录任何合并范围元数据，空洞单元格在清洗中被当空行挤掉。

**失败用例**（P1.2）：

```python
def test_excel_preserves_merged_cell_span(workbook_bytes):
    # A1:B1 合并，值为 "检验结果"
    result = ExcelParser().parse(workbook_bytes, ".xlsx")
    block = result.blocks[0]
    # 期望 block 携带 merged_ranges={"A1:B1": "检验结果"}，且 bridging 值不丢。
    assert block.merged_ranges == {"A1:B1": "检验结果"}
    assert "检验结果" in block.raw_text
```

**影响**：合并表头/跨列单元格被截断，语义数据丢失。

**P1.2 方向**：非 `read_only` 读取 `ws.merged_cells.ranges`；空洞单元格回填合并值，
并把合并范围写进块元数据（不占用 cleaning flag 词汇）。

---

## Gap 4 — cell_range 非真实 used range

**目标**：`cell_range` 应反映工作表**真实使用区间**（含列宽未知的稀疏数据/偏移起始，
如从 B2 开始的表），而非从 A1 强推。

**现状**：`cell_range = f"A1:{get_column_letter(width)}{len(normalized_rows)}"` —
总是从 A1 起算、以“最大行宽+非空行数”收尾；前置空列/空行被忽略，`source_anchor`
也随之偏移。

**失败用例**（P1.2）：

```python
def test_excel_cell_range_reflects_true_used_region(workbook_bytes):
    # 表实际在 B2:D4（B2 起），非 A 列
    result = ExcelParser().parse(workbook_bytes, ".xlsx")
    assert result.blocks[0].cell_range == "B2:D4"  # 当前错误为 "A1:C3"
```

**影响**：anchor 定位错误，检索回溯原单元格时指向错误地址。

**P1.2 方向**：用 `ws.calculate_dimension()` / `min_row` 等真实维度；稀疏偏移保留。

---

## Gap 5 — 每 cell 原始类型/元数据缺失

**目标**：块内文本应能区分“这是日期/数字/公式结果/超链接”等语义，供下游证据链路
按类型校验。

**现状**：只有 `str(value)` 字符串，无 cell 类型（`date`/`numeric`/`formula`）、无
公式、无超链接、无样式/底纹等元数据字段。

**失败用例**（P1.2）：

```python
def test_excel_blocks_expose_cell_types(workbook_bytes):
    result = ExcelParser().parse(workbook_bytes, ".xlsx")
    block = result.blocks[0]
    # 期望按细胞坐标记录类型，如 {"B2": "numeric", "C2": "date"}
    assert block.cell_types["B2"] == "numeric"
```

**影响**：医疗报告里“数值”与“文本”混淆会污染数值证据。

**P1.2 方向**：为非 `read_only` 加载的 cell 维护 `{ coord: type }` 元数据映射。

---

## 非缺口确认（Task 3 已交付）

- `source_anchor` 精确为 `sheet:{sheet_name}!{cell_range}`，且 `sheet_name` / 
  `cell_range` 原样透传到 block。
- OCR / PDF / DOCX 的版面块增量输出正常，legacy `segments`/`tables` 未破坏
  （`--ignore` 全量 205 passed）。
- `parser_version = "spreadsheet-parser-v2"` 已透传到 `ParseResult`。

---

## 附：PDF 逐页 OCR 回退的显式延时（Task 3 Step 3 / Path B）

> 本附录与 Excel 无关，但同属 Task 3 的 P1.2 积压输入，故一并归档于此。

**现状决策（Path B）**：`should_ocr_page(blocks, page_number)` 是真实可触达的逐页
接缝，服务端对 PDF 逐页扫描缺文本页：
- 无缺文本页 → 保留原生解析；
- 全部页缺文本 → 整份回退 OCR（无原生可丢）；
- **混合（部分页缺、部分页有原生文本）** → 抛 `ValueError` 明确失败，**绝不静默丢页**
  （例如第 1/3 页有原生文本、第 2 页空白/扫描，不会悄悄丢掉第 2 页）。

**为何不逐页 OCR**：现行 `OCRParser` 协议只接受整份 `file: BinaryIO`，且 OCR 后端
不保留页身份（`_build_ocr_blocks` 恒置 `page_number=1`）；仓库无 PDF→图像渲染器
（无 pymupdf/pdf2image/pypdfium2，仅 PIL/Pillow），无法“渲染页 N 再单页 OCR”。
因此“只 OCR 缺文本页”在现行后端下不可行。

**P1.2 / 后续任务输入**：
- **Task 7（操作出口）**：将上述混合 PDF 的 `ValueError` 接入操作出口 —— 提示用户
  拆分 PDF 或明确降级策略，而不是在 `process_file` 直接异常中断作业。
- **Task 11（页级 OCR 后端）**：引入页渲染（如 pypdfium2/pymupdf）与保留页身份的
  OCR 后端后，把 `_apply_pdf_ocr_fallback` 的混合分支从“抛异常”升级为“逐页补扫
  OCR”，并以正确的 `page_number` 与 `p{page}` 页级唯一锚点合并进结果。