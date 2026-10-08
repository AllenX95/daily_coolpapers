"""Isolated browser fixture: temporary databases, mocked HTTP, worker without scheduler."""
import argparse
import json
import threading
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import httpx
from daily_coolpapers import db,llm
from tests.test_exploration import ExplorationTests


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18773)
    args=parser.parse_args()
    case=ExplorationTests('test_five_calls_checkpoint_usage_and_no_business_side_effects')
    case.setUp()
    worker=None
    try:
        ids=case.papers(45);case.configure()
        with db.connect() as conn:
            conn.execute('UPDATE papers SET created_at=?',((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
        def respond(request):
            body=json.loads(request.content)
            if body['max_tokens']==1000:
                raw=body['messages'][1]['content'].split('论文数据：',1)[1].split('\n输出',1)[0]
                result=case.extract(json.loads(raw))
            else:
                # Use only successful IDs supplied by the synthesis prompt.
                raw=body['messages'][1]['content'].split('原始论文：',1)[1].split('\n输出',1)[0]
                papers=json.loads(raw)
                result=case.synthesis([p['id'] for p in papers])
                result['candidates'][0].update(name='跨任务持久记忆',scope_text='面向长期任务的状态记忆与检索，排除简单对话记录包装。',
                    why_now='隔离测试中的两篇论文提出了不同的记忆管理方法。',difference_from_existing='可作为现有 Agent 研究的子方向。',
                    evidence_gaps='摘要不能确认长周期表现与独立复现。')
                result['summary']='隔离演示：发现一个值得继续观察的候选。'
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}],
                'usage':{'prompt_tokens':30,'completion_tokens':15}},request=request)
        case.stack.enter_context(patch.object(llm,'_api_key',return_value='fixture'))
        case.stack.enter_context(patch.object(llm,'make_llm_client',side_effect=lambda _:httpx.Client(transport=httpx.MockTransport(respond))))
        worker=threading.Thread(target=case.runner._worker_loop,daemon=True)
        worker.start()
        print('Exploration fixture: mock provider, worker only, scheduler disabled.',flush=True)
        print('Temporary database:',db.DB_PATH,flush=True)
        case.app.run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False)
    finally:
        case.runner._stop_event.set()
        if worker:worker.join(5)
        case.doCleanups()


if __name__=='__main__':main()
