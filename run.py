"""WSGI entrypoint: use ``run:app`` with Gunicorn for deployment."""

import os

from auction.app import create_app

app = create_app()


def main() -> None:
    """Run Flask's local server using the configured app and optional ``PORT``.

    Deployment imports ``app`` through Gunicorn instead of using this server.
    Invalid port values fail during integer conversion before binding a socket.
    """
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=False, threaded=True)


if __name__ == "__main__":
    main()
