"""Isolated B-first-stage browser fixture; never starts production runtime."""
import argparse

from daily_coolpapers import db
from tests.test_personal_library import PersonalLibraryTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18766)
    args = parser.parse_args()
    case = PersonalLibraryTests('test_history_stays_undecided_and_favorites_are_explicit')
    case.setUp()
    try:
        first = case.paper(1, title='Agent Memory: A Research Starting Point', score=88)
        case.evaluate(first, 'failed')
        case.paper(2, title='Unreviewed Paper', status=None)
        skipped = case.paper(3, title='Efficient Reasoning: A Skipped Approach', score=72)
        db.set_paper_decision(skipped, 'skipped')
        case.paper(4, title='World Models: An Open Question', score=81)
        print('Temporary library fixture; no worker, scheduler, crawler or LLM.', flush=True)
        case.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False)
    finally:
        case.doCleanups()


if __name__ == '__main__':
    main()
