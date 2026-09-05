"""Module C browser fixture: synthetic papers, temporary SQLite, no runtime."""
import argparse

from daily_coolpapers import db
from tests import test_personal_library as library


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18768)
    args = parser.parse_args()
    case = library.PersonalLibraryTests('test_history_stays_undecided_and_favorites_are_explicit')
    case.setUp()
    try:
        first = case.paper(1, title='Agent Memory: Team Research Fixture', score=88)
        case.evaluate(first, 'failed')
        case.paper(2, title='Efficient Reasoning: A Follow-up', score=82)
        case.paper(3, title='Unreviewed Team Paper', status=None)
        db.set_paper_decision(first, 'skipped')
        theme_id = db.create_investment_theme('Agent research')
        db.set_paper_investment_themes(first, [theme_id])
        print('Temporary team fixture; no worker, scheduler, crawler or LLM.', flush=True)
        print('Fixture database:', db.DB_PATH, flush=True)
        case.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False)
    finally:
        case.doCleanups()


if __name__ == '__main__':
    main()
