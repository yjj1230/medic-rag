from opensearchpy import OpenSearch
import json
c=OpenSearch('http://localhost:9200')
terms=['视力改变','视觉改变','视物异常','一次测量','单次测量','不能单独完成诊断','不能凭一次读数']
out={}
for t in terms:
    body={'size':20,'query':{'bool':{'must':[{'match':{'text':{'query':t,'operator':'or'}}}],'filter':[{'term':{'visibility':'public'}},{'term':{'retrieval_eligible':True}}]}}}
    out[t]=[{'id':h['_id'],'score':h['_score']} for h in c.search(index='medical-rag-public-local',body=body)['hits']['hits']]
print(json.dumps(out,ensure_ascii=False,indent=2))
