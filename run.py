"""WSGI entrypoint: use ``run:app`` with Gunicorn for deployment."""

import os

from auction.app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), debug=False, threaded=True)
