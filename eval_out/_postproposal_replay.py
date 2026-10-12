import json
run=json.load(open('eval_out/test_v2_confirmation_after_fix/retrieval_eval_results.json',encoding='utf-8'))
cfg=next(x for x in run['configs'] if x['name']=='A3-hybrid-rrf60')
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
for row in cfg['cases']:
    cid=row['case_id']
    if cid not in groups: continue
    returned=row['returned_ids']
    ranks=[]
    matched=[]
    for g in groups[cid]:
        rs=[returned.index(i)+1 for i in g if i in returned]
        if rs:
            ranks.append(min(rs)); matched.append({'group':g,'rank':min(rs)})
    first=min(ranks) if ranks else None
    rows.append({'case_id':cid,'returned_ids':returned,'accepted_groups':groups[cid],'matched_groups':matched,'accepted_hit':bool(ranks),'first_accepted_rank':first,'accepted_reciprocal_rank':(1/first if first else 0.0)})
summary={'dataset':'eval/rag_cases.test.v2.jsonl','configuration':'A3-hybrid-rrf60','frozen_dataset_unchanged':True,'note':'仅按人工 revision proposal 和新来源的直接支持证据重放，不修改冻结 gold，也不作为正式 Test v2 指标。','case_count':len(rows),'accepted_hit_rate':sum(x['accepted_hit'] for x in rows)/len(rows),'accepted_mrr':sum(x['accepted_reciprocal_rank'] for x in rows)/len(rows),'cases':rows}
open('eval_out/test_v2_postproposal_quality.json','w',encoding='utf-8').write(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps({k:summary[k] for k in ('case_count','accepted_hit_rate','accepted_mrr')},ensure_ascii=False))
