from opensearchpy import OpenSearch
import json
c=OpenSearch('http://localhost:9200')
tests=[('emergency','高血压伴视力变化为什么需要升级处理？',['视力改变']),('symptom','一次家庭读数可以直接确诊高血压吗？',['不能凭一次测量','不能凭一次读数','不能单独完成诊断'])]
out=[]
for name,q,terms in tests:
    should=[{'multi_match':{'query':q,'fields':['text'],'type':'best_fields'}},{'match_phrase':{'text':{'query':q,'boost':2.0}}}]
    should.extend({'match':{'text':{'query':t,'operator':'and','boost':12.0}}} for t in terms)
    body={'size':10,'query':{'bool':{'should':should,'minimum_should_match':1,'filter':[{'term':{'visibility':'public'}},{'term':{'retrieval_eligible':True}}],'must_not':[{'term':{'deleted':True}},{'term':{'source_active':False}}]}}}
    hits=c.search(index='medical-rag-public-local',body=body)['hits']['hits']
    out.append({'name':name,'hits':[{'id':h['_id'],'score':h['_score']} for h in hits]})
print(json.dumps(out,ensure_ascii=False,indent=2))
