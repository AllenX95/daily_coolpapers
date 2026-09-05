"""Optional isolated browser fixture. Never starts the production runtime/worker."""
import argparse
from tests.test_job_center import JobCenterTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    case = JobCenterTests('test_detail_and_index_keep_completed_partial_results')
    case.setUp()
    try:
        job = case.completed(partial=True)
        print(f"Synthetic job #{job['id']}; isolated DB; scheduler and external calls disabled", flush=True)
        case.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False)
    finally:
        case.doCleanups()


if __name__ == '__main__':
    main()
