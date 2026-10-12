import json, hashlib
from pathlib import Path
src=Path('eval/rag_cases.test.v2.jsonl')
out=Path('eval/rag_cases.test.v2.1.jsonl')
canonical={
 'who-silent':['c215f4b2-8c97-47b4-9d68-fca55beb14a3'],
 'who-numbers':['26af9567-28d0-4e0c-a48e-2bdb3065dc06'],
 'who-organ-risk':['1dba0b0c-0bcd-4f30-b1e2-938ae5a3e5f2'],
 'lifestyle-exercise':['4d34b07f-7cae-47f6-9d9d-71b1663a4f5f'],
 'lifestyle-salt':['39f7ae94-cf94-49f2-9ba3-9a9ed77bf1f1'],
 'meds-stop':['7ebdec91-4ed7-46dc-8bf6-da34df585366'],
 'emergency-vision':['8c09dd65-8322-4ffd-b665-73d136b31b7a'],
 'symptom-single':['c215f4b2-8c97-47b4-9d68-fca55beb14a3'],
 'who-sodium':['b47cac0f-c442-4499-9ac4-44cfc89b9717'],
 'risk-check':['c215f4b2-8c97-47b4-9d68-fca55beb14a3'],
 'priv-hgb-low':['2a555496-ac1d-4c2f-b0b8-b1c5909386bb'],
 'priv-report-followup':['822690e4-42ad-474d-9851-4f6462c23168'],
 'priv-advice-safety':['2a555496-ac1d-4c2f-b0b8-b1c5909386bb'],
}
groups={
 'who-silent':[['c215f4b2-8c97-47b4-9d68-fca55beb14a3'],['1f18737d-6471-4de4-a790-867fdf9034b8']],
 'who-numbers':[['26af9567-28d0-4e0c-a48e-2bdb3065dc06']],
 'who-organ-risk':[['1dba0b0c-0bcd-4f30-b1e2-938ae5a3e5f2']],
 'lifestyle-exercise':[['4d34b07f-7cae-47f6-9d9d-71b1663a4f5f'],['39f7ae94-cf94-49f2-9ba3-9a9ed77bf1f1']],
 'lifestyle-salt':[['39f7ae94-cf94-49f2-9ba3-9a9ed77bf1f1'],['1f18737d-6471-4de4-a790-867fdf9034b8']],
 'meds-stop':[['7ebdec91-4ed7-46dc-8bf6-da34df585366'],['4d34b07f-7cae-47f6-9d9d-71b1663a4f5f']],
 'emergency-vision':[['8c09dd65-8322-4ffd-b665-73d136b31b7a'],['1f18737d-6471-4de4-a790-867fdf9034b8']],
 'symptom-single':[['c215f4b2-8c97-47b4-9d68-fca55beb14a3'],['1f18737d-6471-4de4-a790-867fdf9034b8']],
 'who-sodium':[['b47cac0f-c442-4499-9ac4-44cfc89b9717']],
 'risk-check':[['c215f4b2-8c97-47b4-9d68-fca55beb14a3'],['1f18737d-6471-4de4-a790-867fdf9034b8']],
 'priv-hgb-low':[['822690e4-42ad-474d-9851-4f6462c23168','2a555496-ac1d-4c2f-b0b8-b1c5909386bb']],
 'priv-report-followup':[['822690e4-42ad-474d-9851-4f6462c23168','2a555496-ac1d-4c2f-b0b8-b1c5909386bb']],
 'priv-advice-safety':[['822690e4-42ad-474d-9851-4f6462c23168','2a555496-ac1d-4c2f-b0b8-b1c5909386bb']],
}
rows=[]
for line in src.read_text(encoding='utf-8').splitlines():
    if not line.strip(): continue
    d=json.loads(line)
    cid=d['id']
    if cid in canonical:
        d['gold_evidence_ids']=canonical[cid]
        d['gold_evidence_groups']=groups[cid]
        d['revision_of']='rag_cases.test.v2.jsonl'
        d['revision_reason']='人工误差分析确认：等价官方证据或新增 WHO 直接支持证据'
    rows.append(d)
out.write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in rows)+'\n',encoding='utf-8')
sha=hashlib.sha256(out.read_bytes()).hexdigest()
manifest={
 'dataset':'eval/rag_cases.test.v2.1.jsonl',
 'revision_of':'eval/rag_cases.test.v2.jsonl',
 'created_at':'2026-10-08',
 'status':'revision_candidate',
 'case_count':len(rows),
 'category_counts':{k:sum(1 for x in rows if x.get('category')==k) for k in ('quality','rejection','safety')},
 'revised_case_count':len(canonical),
 'gold_evidence_groups':True,
 'knowledge_snapshot':{'public_index_docs':30,'private_index_docs':2,'public_source_count':10,'who_explicit_source_id':'be9ec2fe-797c-411b-a4cc-acead437bdc6','reviewer':'俞家俊'},
 'sha256':sha,
 'notes':'在冻结 Test v2 基础上建立的候选修订集；不覆盖原始冻结集。等价证据使用 gold_evidence_groups 表达，当前兼容评估脚本使用每条 case 的 canonical gold_evidence_ids。'
}
Path('eval/rag_cases.test.v2.1.manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'cases':len(rows),'revised':len(canonical),'sha256':sha},ensure_ascii=False))
