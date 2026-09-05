"""Isolated memo browser fixture: temporary SQLite, mocked LLM, worker only."""
import argparse
import json
import threading

from daily_coolpapers import db
from daily_coolpapers.llm import LLMResponse
from tests import test_memo_foundation as foundation
from tests.test_memo_generation import valid_result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18770)
    args=parser.parse_args()
    case=foundation.MemoFoundationTests('test_creation_freezes_order_latest_success_evaluations_and_one_job')
    case.setUp()
    try:
        for number,title,score in [(1,'Agent Memory Systems for Durable Workflows',91),(2,'Efficient Multimodal Reasoning at the Edge',86),(3,'Robotics World Models under Distribution Shift',78)]:
            paper=case.favorite(number)
            with db.connect() as conn:
                conn.execute('UPDATE papers SET title=? WHERE id=?',(title,paper))
            case.evaluate(paper,'success',kind='abstract_review',score=score-4)
        direction=db.create_attention_direction('Agent 基础设施','面向企业 Agent 的记忆、工具调用和可靠工作流。')
        db.set_direction_decision(1,direction,'confirmed')
        case.llm.side_effect=None
        result=valid_result(['P1'])
        case.llm.return_value=LLMResponse(json.dumps(result,ensure_ascii=False),result,{'input_tokens':321,'output_tokens':123})
        runner=case.runner
        runner._started=True
        runner._stop_event.clear()
        runner._worker_thread=threading.Thread(target=runner._worker_loop,name='memo-fixture-worker',daemon=True)
        runner._worker_thread.start()
        print('Temporary memo fixture; worker only, mocked LLM, no scheduler/crawler/network.',flush=True)
        print('Fixture database:',db.DB_PATH,flush=True)
        case.app.run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False)
    finally:
        case.runner.stop()
        case.doCleanups()


if __name__=='__main__':
    main()
