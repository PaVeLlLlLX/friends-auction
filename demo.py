"""Seed a separate local demo with invented items and no real personal information."""
import os
import time
from pathlib import Path
os.environ['AUCTION_DEMO'] = '1'
os.environ['AUCTION_DB'] = str(Path(__file__).resolve().parent / 'data' / 'demo.sqlite3')
os.environ['AUCTION_UPLOADS'] = str(Path(__file__).resolve().parent / 'data' / 'demo-photos')
from auction.app import create_app, connect
app = create_app()
db = connect(app.config['DB'])
if not db.execute('SELECT 1 FROM lots LIMIT 1').fetchone():
    db.executemany('INSERT INTO lots(title,description,condition,start_price,step,created) VALUES(?,?,?,?,?,?)', [
        ('Бокалы для вина', 'Набор из четырёх бокалов.', 'Отличное', 0, 50, time.time()),
        ('Акустическая гитара', 'Акустическая гитара в рабочем состоянии.', 'Бережное использование', 500, 100, time.time()),
        ('Парфюм', 'Почти полный флакон.', 'Открыт', 100, 50, time.time()),
    ])
# Update only the exact old sample text; retain any edits made by the user.
for before, after in (
    ('Набор из четырёх бокалов. Будут рады долгим ужинам и хорошей компании.', 'Набор из четырёх бокалов.'),
    ('Хорошая гитара для первых аккордов и вечеров с друзьями.', 'Акустическая гитара в рабочем состоянии.'),
    ('Почти полный флакон. Расскажу подробнее при встрече.', 'Почти полный флакон.'),
):
    db.execute('UPDATE lots SET description=? WHERE description=?', (after, before))
db.close()
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8080, debug=False, threaded=True)
