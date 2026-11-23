from contextlib import closing
import json
from pathlib import Path
import sqlite3
from app.faults import DockFault
from app.seals import compact


class Dockets:
    """放行台账：一份归档对应一个放行台，决定与出库同事务落库。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS dockets ('
                       'receipt_id TEXT PRIMARY KEY, current_id TEXT NOT NULL, '
                       'flags TEXT NOT NULL, decided_at TEXT, created_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS release_orders ('
                       'id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, decided_at TEXT NOT NULL, '
                       'lines TEXT NOT NULL, outbound TEXT NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def open(self, receipt_id, current_id, created_at):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO dockets(receipt_id,current_id,flags,created_at) VALUES (?,?,?,?)',
                       (receipt_id, current_id, '[]', created_at))
        return self.get(receipt_id)

    def get(self, receipt_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT current_id,flags,decided_at,created_at FROM dockets WHERE receipt_id=?',
                             (receipt_id,)).fetchone()
            if row is None:
                raise DockFault('NO_DOCKET', '该归档尚未开台放行', 404)
            orders = [dict(zip(('id', 'receipt_id', 'decided_at', 'lines', 'outbound'),
                               (key, rid, at, json.loads(lines), json.loads(outbound))))
                      for key, rid, at, lines, outbound in db.execute(
                          'SELECT id,receipt_id,decided_at,lines,outbound FROM release_orders ORDER BY rowid')
                      if rid == receipt_id]
        return dict(receipt_id=receipt_id, current_id=row[0], flags=json.loads(row[1]),
                    decided_at=row[2], created_at=row[3], orders=orders)

    def order(self, order_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT id,receipt_id,decided_at,lines,outbound FROM release_orders WHERE id=?',
                             (order_id,)).fetchone()
        if row is None:
            raise DockFault('NO_ORDER', '放行单不存在', 404)
        return dict(zip(('id', 'receipt_id', 'decided_at', 'lines', 'outbound'),
                        (row[0], row[1], row[2], json.loads(row[3]), json.loads(row[4]))))

    def save_order(self, order_id, receipt_id, decided_at, lines, outbound):
        """决定与出库同事务落库；重复单号被忽略，返回是否首次写入。"""
        with closing(self.connect()) as db, db:
            cursor = db.execute('INSERT OR IGNORE INTO release_orders VALUES (?,?,?,?,?)',
                                (order_id, receipt_id, decided_at, compact(lines), compact(outbound)))
            created = cursor.rowcount == 1
            if created:
                db.execute('UPDATE dockets SET decided_at=? WHERE receipt_id=?', (decided_at, receipt_id))
        return created

    def reevaluate(self, receipt_id, current_id, flags):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE dockets SET current_id=?, flags=? WHERE receipt_id=?',
                       (current_id, compact(flags), receipt_id))
        return self.get(receipt_id)
