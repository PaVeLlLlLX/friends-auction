import os
from auction.app import create_app

app = create_app()

if __name__ == '__main__':
    if not app.config['DEMO'] and (not app.config['BOT_TOKEN'] or not app.config['ADMIN_ID']):
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID, or use AUCTION_DEMO=1 locally.')
    # Demo is deliberately bound to loopback and is never a production mode.
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8080')), debug=False, threaded=True)
