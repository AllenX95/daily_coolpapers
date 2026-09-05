"""Synthetic module D fixture, mock LLM and a worker only (never a scheduler)."""
import argparse
import json
import threading
from contextlib import nullcontext
from unittest.mock import patch

from daily_coolpapers import db, services
from daily_coolpapers.llm import LLMError, LLMResponse
from tests import test_personal_library as library


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18769)
    args = parser.parse_args()
    case = library.PersonalLibraryTests('test_history_stays_undecided_and_favorites_are_explicit')
    case.setUp()
    thread = None
    try:
        for number,title in enumerate(('D Fixture 01 Memory','D Fixture 02 Adjacent','D Fixture 03 Other','D Fixture 04 Failure'),1):
            case.paper(number,title=title,status=None)
        db.save_llm_profile({'name':'Fixture only','provider':'openai_compatible','base_url':'https://example.invalid',
                            'model':'synthetic','enabled':1,'is_default_classification':1,'is_default_abstract':1})
        def respond(_profile,prompt,**_kwargs):
            if '方向定义：' in prompt:
                if 'D Fixture 04' in prompt:
                    raise LLMError('Synthetic terminal error')
                direction_data = prompt.split('方向定义：',1)[1].split('论文 Metadata：',1)[0].strip()
                directions = json.loads(direction_data)
                state = 'matched' if 'D Fixture 01' in prompt else ('possible' if 'D Fixture 02' in prompt else 'unmatched')
                result = {'directions':[{'direction_id':d['id'],'decision':state,'reason':'仅用隔离的原始摘要作模拟分类。'} for d in directions]}
                return LLMResponse(json.dumps(result),result,{'input_tokens':20,'output_tokens':10})
            return LLMResponse('{}',{'score':80,'attention':'read','one_sentence_summary':'隔离摘要评估样本'}, {'input_tokens':30,'output_tokens':10})
        case.llm.side_effect = respond
        case.stack.enter_context(patch.object(services,'make_llm_client',side_effect=lambda _profile:nullcontext(object())))
        case.stack.enter_context(patch.object(services,'_abstract_retry_wait'))
        thread = threading.Thread(target=case.runner._worker_loop,daemon=True)
        thread.start()
        print('Module D fixture: mock LLM, worker only, scheduler/crawler disabled.',flush=True)
        print('Fixture database:',db.DB_PATH,flush=True)
        case.app.run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False)
    finally:
        case.runner._stop_event.set()
        if thread:
            thread.join(5)
        case.doCleanups()


if __name__ == '__main__':
    main()
