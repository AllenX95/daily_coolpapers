"""Isolated B-second-stage browser fixture; never starts production runtime."""
import argparse

from daily_coolpapers import db
from tests import test_personal_library as library


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18767)
    args = parser.parse_args()
    case = library.PersonalLibraryTests('test_history_stays_undecided_and_favorites_are_explicit')
    case.setUp()
    try:
        first = case.paper(1, title='Agent Memory: A Research Starting Point', score=88)
        case.evaluate(first, 'failed')
        case.paper(2, title='Unreviewed Paper', status=None)
        skipped = case.paper(3, title='Efficient Reasoning: A Skipped Approach', score=72)
        db.set_paper_decision(skipped, 'skipped')
        print('Temporary theme fixture; no worker, scheduler, crawler or LLM.', flush=True)
        print('Fixture database:', db.DB_PATH, flush=True)
        case.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False)
    finally:
        case.doCleanups()


if __name__ == '__main__':
    main()
