# 高血压医疗 RAG：公开权威文档清单

> 研究日期：2026-10-08（Asia/Shanghai）  
> 目标：为当前医疗 RAG 选取高血压、家庭血压监测、降压药安全、高血压急症和生活方式干预的一手/官方资料。  
> 访问检查：研究时逐一请求官方页面；表中页面均为公开 HTML（无需登录或付费）。WHO 的 IRIS PDF 链接由官方页面发布，但本环境对 IRIS 直链返回 403，需通过 WHO 页面/IRIS 下载流程获取。  
> 使用边界：以下是知识源候选，不替代医学审核；入库前应保存来源 URL、发布日期/版本、抓取时间和许可信息，并为每个版本设置失效/复核日期。

## 推荐优先级

- **Tier A（许可较清晰，可在履行署名和内容筛选后进入公共知识库）**：WHO 2021 指南；CDC 两页；NHLBI 四页；MedlinePlus 高血压主题页。仅收录正文文本，排除第三方图片、视频、嵌入组件和外链全文。
- **Tier B（公开可读，但用于 AI/RAG 前必须取得或确认许可）**：NICE NG136、NG133。NICE 的 2026 UK Open Content Licence 明确规定 AI 用途不在该许可覆盖范围内，应先联系 `reuseofcontent@nice.org.uk` 或按其 API 申请流程取得许可。

## 文档目录

| ID | 主题 | 文档（机构） | 日期/版本 | 官方来源与格式 | 许可/再利用提示 | 建议知识库用途 | 下载与风险 |
|---|---|---|---|---|---|---|---|
| WHO-HTN-2021 | 降压药、目标值、联合治疗、随访 | **Guideline for the pharmacological treatment of hypertension in adults**（World Health Organization） | 2021-08-24；ISBN 978-92-4-003398-6；48 页 | [WHO 出版物页](https://www.who.int/publications/i/item/9789240033986)（HTML）；官方 IRIS 内容链接见页面 `sameAs`：[PDF/内容流](https://iris.who.int/server/api/core/bitstreams/f062769d-f075-4a00-87af-0a2106e0bd04/content) | WHO 元数据标注 **CC BY-NC-SA 3.0 IGO**（[许可证](https://creativecommons.org/licenses/by-nc-sa/3.0/igo/)）。需署名、非商业、相同方式共享；改编须标明修改，不能暗示 WHO 背书。 | 作为成人高血压药物治疗的主指南：一线药物类别、启动/强化、联合用药、血压目标和实施监测。适合回答“为什么用药/何时复诊/如何长期管理”。 | 官方出版物页可公开访问；IRIS 直链在部分网络环境会被 403/反爬拦截。建议保留官方 HTML 元数据和 PDF 哈希；不要从第三方镜像下载。 |
| NICE-NG136 | 诊断、家庭血压监测、生活方式、成人用药 | **Hypertension in adults: diagnosis and management (NG136)**（National Institute for Health and Care Excellence, UK） | 发布 2019-08-28；页面标注最后更新 2026-02-26 | [NICE 指南页](https://www.nice.org.uk/guidance/ng136)；[Recommendations](https://www.nice.org.uk/guidance/ng136/chapter/recommendations)（HTML）；页面提供 guidance PDF 下载 | 受 [NICE UK Open Content Licence](https://www.nice.org.uk/reusing-our-content/nice-uk-open-content-licence) 约束：需固定格式署名和免责声明；单条建议/质量声明/实质算法不得改写结构或措辞；标识、第三方内容另受限。**AI 用途不在该许可内**，需事先申请；国际商业/非个人研究使用可能另收费。 | 在取得 AI 许可后，用于 ABPM/HBPM 确诊路径、诊室/家庭目标、生活方式建议、成人阶梯用药和随访规则。 | HTML/PDF 均可公开读取；许可风险高于 WHO。未获许可前只保存元数据和官方链接，或由医学审核人员依据原文撰写不复制原文的内部摘要；不要批量抓取全文。 |
| NICE-NG133 | 妊娠/备孕时降压药安全 | **Hypertension in pregnancy: diagnosis and management (NG133)**（NICE） | 发布 2019-06-25；最后更新 2023-04-17 | [NICE 指南页](https://www.nice.org.uk/guidance/ng133)；[Recommendations](https://www.nice.org.uk/guidance/ng133/chapter/recommendations)（HTML）；页面提供 PDF | 同 NG136：NICE UK Open Content Licence；**AI/RAG 使用须单独确认许可**。指南建议应按原文版本保存，禁止未经许可改写成新的“官方建议”。 | 高风险用药安全专题：ACEI/ARB 备孕和妊娠风险提醒、停换药时机、孕期可选药物和子痫前期管理。应设置“孕妇/备孕”路由和强制就医提示。 | 公开 HTML/PDF；内容针对孕期，不能与普通成人用药规则混用。许可和版本漂移风险高，需按 NICE update 信息重新索引。 |
| CDC-BP-ABOUT | 定义、风险、症状、预防 | **About High Blood Pressure**（Centers for Disease Control and Prevention, CDC） | 页面元数据 `cdc:last_updated`：2026-01-28 | [CDC 页面](https://www.cdc.gov/high-blood-pressure/about/index.html)（HTML） | CDC [Use of Agency Materials](https://www.cdc.gov/other/agencymaterials.html)：大多数正文为美国政府公共领域，可复制；需注明来源并加不背书免责声明。承包商/第三方内容、图片可能受版权保护；CDC/HHS 标志不得擅自使用。美国以外的版权保护可能不同。 | 面向患者的基础解释：什么是高血压、为何需要复测和治疗、风险因素、预防入口。适合生成低风险健康教育和查询改写词表。 | HTML；可由官方页面打印为 PDF，但不要把生成 PDF 当作独立版本。只抽取正文和页面日期，过滤图片、社交组件和第三方链接。 |
| CDC-BP-MEASURE | 家庭血压测量方法 | **Measuring Your Blood Pressure**（CDC） | 发布 2024-05-15；更新 2026-09-04；页面注明专家复核 2024-12-13 | [CDC 页面](https://www.cdc.gov/high-blood-pressure/measure/index.html)（HTML） | 同 CDC 公共领域政策：正文通常可复用，署名和免责声明必需；图片、第三方素材、CDC 标志单独受限。 | 家庭监测操作：测量前准备、坐姿/手臂位置、影响读数的因素、何时在家测量并记录。适合拆成步骤型 chunk 和“测量错误纠正”问答。 | HTML；页面可能持续小幅更新，抓取时记录 `Published/Updated/Reviewed` 三个字段；不得把 CDC 页面阈值与其他指南阈值混为一谈。 |
| NHLBI-BP-OVERVIEW | 血压分级、基本概念 | **High Blood Pressure – What Is High Blood Pressure?**（National Heart, Lung, and Blood Institute, NIH） | 最后更新 2024-04-25 | [NHLBI 页面](https://www.nhlbi.nih.gov/health/high-blood-pressure)（HTML） | NHLBI 属美国联邦机构；应按 [NLM Web Policies](https://www.nlm.nih.gov/web_policies.html) 处理：美国政府作品在美国可免费复制，需注明 NLM/NIH 来源；页面内第三方图像、动画、链接内容可能另有版权。 | 血压数值/分级的基础知识、长期风险和术语解释。适合建立“收缩压/舒张压/高血压”等规范词和患者友好释义。 | HTML；仅抽取文字。页面含 Nucleus Medical Media 等第三方动画版权提示，禁止把动画或图片入库。 |
| NHLBI-BP-TREAT | 生活方式与药物治疗 | **High Blood Pressure – Treatment**（NHLBI/NIH） | 最后更新 2024-04-30 | [NHLBI Treatment](https://www.nhlbi.nih.gov/health/high-blood-pressure/treatment)（HTML） | 同 NLM 政府作品政策；第三方图像/动画/工具另行核权。 | DASH/低盐、运动、限酒、体重管理，以及常见降压药类别、依从性和副作用沟通。适合“生活方式+用药”联合回答，但药物具体剂量必须转诊药师/医生。 | HTML；按章节拆分。对“降到多少可以停药”“漏服是否加倍”等问题不应从该页推断，需回到药品说明书或临床指南。 |
| NHLBI-BP-LIVING | 高血压急症识别、安全升级 | **High Blood Pressure – Living With High Blood Pressure**（NHLBI/NIH） | 最后更新 2024-04-30 | [NHLBI Living With](https://www.nhlbi.nih.gov/health/high-blood-pressure/living-with)（HTML） | 同 NLM 政府作品政策；文字可在美国政府作品范围内复用并注明来源；第三方内容另行核权。 | 急症安全网：页面写明读数高于 **180/120 mmHg** 且伴突发剧烈头痛、呼吸困难、胸/背/腹剧痛、麻木无力、视力改变或言语障碍时立即拨打急救电话。适合安全分类器和就医升级模板。 | HTML；将“阈值 + 伴随症状 + 立即行动”作为不可拆散的证据块。不能把无症状单次高读数直接等同于急症，需按页面上下文复测并联系医护人员。 |
| NHLBI-BP-CAUSES | 生活方式、风险因素、升压药物 | **High Blood Pressure – Causes and Risk Factors**（NHLBI/NIH） | 最后更新 2024-04-30 | [NHLBI Causes](https://www.nhlbi.nih.gov/health/high-blood-pressure/causes)（HTML） | 同 NLM 政府作品政策；排除第三方图片/材料。 | 盐/钠、酒精、运动、睡眠、压力、体重，以及可能升高血压的药物（如部分减充血剂、NSAIDs 等）风险教育。适合生活方式干预和“某药会不会升高血压”的检索入口。 | HTML；药物风险仅作为筛查提示，不能据此让患者自行停药；回答应建议核对具体药品说明书和医生意见。 |
| MEDLINEPLUS-HTN | 综合患者教育与权威外链 | **High Blood Pressure (Hypertension)**（MedlinePlus, U.S. National Library of Medicine/NIH） | 最后更新 2026-01-22 | [MedlinePlus 主题页](https://medlineplus.gov/highbloodpressure.html)（HTML） | [NLM Web Policies](https://www.nlm.nih.gov/web_policies.html)：美国政府作品可在美国免费复制并注明 NLM 来源；页面链接的非政府资源、图片、视频和 Medical Encyclopedia 内容可能受第三方版权保护。 | 作为患者语言索引和导航层：症状、诊断、治疗、预防、生活方式和相关专题的官方链接聚合；不作为唯一治疗决策来源。 | HTML；建议只索引 NLM 自有正文和链接标题/摘要。不要抓取并重发布 MedlinePlus Medical Encyclopedia（其页面明确含第三方版权限制）。 |

## 主题到文档的映射

| 需求 | 首选证据块 | 备选/补充 | 安全要求 |
|---|---|---|---|
| 高血压定义、风险和分级 | CDC-BP-ABOUT；NHLBI-BP-OVERVIEW | MEDLINEPLUS-HTN | 输出教育性解释，避免把单次读数当作诊断。 |
| 家庭血压监测 | CDC-BP-MEASURE；NICE-NG136（获许可后） | WHO-HTN-2021 的实施/监测部分 | 保留测量步骤、设备/姿势和记录上下文；不要跨指南拼接不同阈值。 |
| 降压药用药安全 | WHO-HTN-2021；NHLBI-BP-TREAT | NICE-NG136；妊娠场景用 NICE-NG133（获许可后） | 不生成个体化剂量/停药指令；孕妇、肾病、药物相互作用问题升级专业人员。 |
| 高血压急症 | NHLBI-BP-LIVING | NICE-NG136（获许可后） | “读数阈值 + 症状 + 行动”必须同块检索；出现神经、胸痛、呼吸困难等症状时优先急救。 |
| 生活方式干预 | NHLBI-BP-TREAT；NHLBI-BP-CAUSES；CDC-BP-ABOUT | WHO-HTN-2021；NICE-NG136（获许可后） | 用来源原有单位和人群范围；不要把普遍建议当作患者个体处方。 |

## 入库和版本治理建议

1. **来源登记**：保存机构、标题、官方 URL、语言、发布日期/最后更新、抓取时间、许可证 URL、内容哈希和人工审核人；同一文档更新时停用旧版本而不是覆盖。
2. **格式策略**：HTML 作为可追踪主格式；WHO/NICE PDF 只在许可允许且能保留页码锚点时入库。抓取正文时保留章节标题、推荐编号和页面日期。
3. **许可门禁**：WHO/CDC/NHLBI/NLM 进入“许可已核验”队列；NICE 进入“AI 使用许可待确认”队列，许可未确认前不得批量全文嵌入或对外生成其原文式回答。
4. **第三方内容过滤**：删除图片、视频、动画、广告、标志、社交组件和外链全文；CDC/NHLBI/NLM 页面中的第三方版权提示必须随源记录保留。
5. **医学审核**：至少按“诊断/阈值、家庭监测、药物安全、急症升级、生活方式”五类建立问答抽查集；对冲突阈值优先展示来源和日期，并要求临床审核。
6. **安全模板**：急症文档按“证据条件→立即行动→不自行调整药物”三段式输出；孕期/备孕和儿童问题单独路由，不由成人通用规则推断。

## 明确排除的公开页面

AHA 2019 血压测量科学声明、AHA 2024 急性医疗场景声明、2017 ACC/AHA 指南和 2024 ESC 指南均有官方公开页面或 DOI，但其页面/出版商元数据未提供可直接确认的开放许可证（通常为期刊/学会版权）。在取得书面许可前，不把这些全文下载或嵌入公共知识库；可仅保存 DOI/链接作为人工查阅参考。

