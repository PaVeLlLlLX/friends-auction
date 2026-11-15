"""Run an isolated local demo with sample lots and simulated accounts."""

import time
from pathlib import Path

from auction.app import connect, create_app
from auction.i18n import translate

ROOT = Path(__file__).resolve().parent


def create_demo_app():
    app = create_app(
        {
            "DEMO": True,
            "TRIAL": False,
            "BOT_TOKEN": "",
            "ADMIN_ID": "",
            "DB": str(ROOT / "data" / "demo.sqlite3"),
            "UPLOADS": str(ROOT / "data" / "demo-photos"),
        }
    )
    db = connect(app.config["DB"])
    try:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM lots LIMIT 1").fetchone():
            db.executemany(
                "INSERT INTO lots(title,description,condition,start_price,step,created) VALUES(?,?,?,?,?,?)",
                [
                    (
                        translate("demo_glasses_title"),
                        translate("demo_glasses_description"),
                        translate("demo_glasses_condition"),
                        0,
                        50,
                        time.time(),
                    ),
                    (
                        translate("demo_guitar_title"),
                        translate("demo_guitar_description"),
                        translate("demo_guitar_condition"),
                        500,
                        100,
                        time.time(),
                    ),
                    (
                        translate("demo_perfume_title"),
                        translate("demo_perfume_description"),
                        translate("demo_perfume_condition"),
                        100,
                        50,
                        time.time(),
                    ),
                ],
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return app


def main():
    create_demo_app().run(host="127.0.0.1", port=8080, debug=False, threaded=True)


if __name__ == "__main__":
    main()
