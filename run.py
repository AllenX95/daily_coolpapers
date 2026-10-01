from daily_coolpapers.app import _flask_secret, create_app, start_runtime
from daily_coolpapers.runtime_lock import RuntimeAlreadyRunningError


app = create_app()


if __name__ == "__main__":
    try:
        runtime = start_runtime()
    except RuntimeAlreadyRunningError as exc:
        raise SystemExit(str(exc)) from None
    try:
        app.secret_key = _flask_secret()
        app.run(host="127.0.0.1", port=8765, debug=False, use_reloader=False)
    finally:
        runtime.stop()
