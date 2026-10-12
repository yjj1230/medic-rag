import json
from pathlib import Path
p=Path('eval/rag_cases.test.v2.1.jsonl')
extra={
 'symptom-risk':[['c215f4b2-8c97-47b4-9d68-fca55beb14a3'],['1f18737d-6471-4de4-a790-867fdf9034b8']],
 'who-medicine':[['1f18737d-6471-4de4-a790-867fdf9034b8'],['2d51bf0e-4382-4899-aa27-000f9fd8741f']],
}
rows=[]
for line in p.read_text(encoding='utf-8').splitlines():
 d=json.loads(line)
 if d['id'] in extra:
  d['gold_evidence_groups']=extra[d['id']]
  d['revision_reason']='人工误差分析确认：新增来源参与排序后，已有等价 WHO 证据仍可支持问题'
 rows.append(d)
p.write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in rows)+'\n',encoding='utf-8')
print('updated',len(rows))
