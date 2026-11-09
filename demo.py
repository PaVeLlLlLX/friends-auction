"""Seed a separate local demo with invented items and no real personal information."""
import os
import time
from pathlib import Path
os.environ['AUCTION_DEMO'] = '1'
os.environ['AUCTION_DB'] = str(Path(__file__).resolve().parent / 'data' / 'demo.sqlite3')
os.environ['AUCTION_UPLOADS'] = str(Path(__file__).resolve().parent / 'data' / 'demo-photos')
from auction.app import create_app, connect
from auction.i18n import translate
app = create_app()
db = connect(app.config['DB'])
if not db.execute('SELECT 1 FROM lots LIMIT 1').fetchone():
    db.executemany('INSERT INTO lots(title,description,condition,start_price,step,created) VALUES(?,?,?,?,?,?)', [
        (translate('demo_module_01'), translate('demo_module_02'), translate('demo_module_03'), 0, 50, time.time()),
        (translate('demo_module_04'), translate('demo_module_05'), translate('demo_module_06'), 500, 100, time.time()),
        (translate('demo_module_07'), translate('demo_module_08'), translate('demo_module_09'), 100, 50, time.time()),
    ])
# Update only the exact old sample text; retain any edits made by the user.
for before, after in (
    (translate('demo_module_10'), translate('demo_module_02')),
    (translate('demo_module_11'), translate('demo_module_05')),
    (translate('demo_module_12'), translate('demo_module_08')),
):
    db.execute('UPDATE lots SET description=? WHERE description=?', (after, before))
db.close()
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8080, debug=False, threaded=True)
