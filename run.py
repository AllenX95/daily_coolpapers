from daily_coolpapers.app import _flask_secret, create_app, start_runtime


app = create_app()


if __name__ == "__main__":
    runtime = start_runtime()
    app.secret_key = _flask_secret()
    try:
        app.run(host="127.0.0.1", port=8765, debug=False, use_reloader=False)
    finally:
        runtime.stop()
