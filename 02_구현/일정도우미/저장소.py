"""단일 프로세스 영속 업무 장부. 모든 공개 DB 진입은 RLock 아래입니다."""
import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from .형식 import ServiceError


def now():
    return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self, path, lock=None):
        self.lock = lock or RLock()
        with self.lock:
            self.conn = sqlite3.connect(str(path), check_same_thread=False)
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('CREATE TABLE IF NOT EXISTS model_calls (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, reserved_at TEXT NOT NULL)')
            self.conn.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            self.conn.execute('CREATE TABLE IF NOT EXISTS sends (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL)')
            self.conn.commit()

    def begin_create(self, job, body, payload_hash):
        with self.lock, self.conn:
            row = self.conn.execute('SELECT payload_hash FROM sends WHERE job_id=?', (job['id'],)).fetchone()
            if row:
                if row[0] != payload_hash:
                    raise ServiceError('payload_changed', '전송 장부와 후보가 다릅니다. 새 등록 없이 기존 ID를 확인하세요.')
                return False
            self.conn.execute('INSERT INTO sends VALUES (?,?,?,?,?)', (job['id'], json.dumps(body, ensure_ascii=False), payload_hash, job['version'], 'unknown'))
            self.conn.execute('UPDATE jobs SET body=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['id']))
            return True

    def send_record(self, job_id):
        with self.lock:
            row = self.conn.execute('SELECT payload,payload_hash,version,state FROM sends WHERE job_id=?', (job_id,)).fetchone()
            return dict(payload=json.loads(row[0]), payload_hash=row[1], version=row[2], state=row[3]) if row else None

    def finish_create(self, job):
        with self.lock, self.conn:
            self.conn.execute('UPDATE sends SET state=? WHERE job_id=?', (job['status'], job['id']))
            self.conn.execute('UPDATE jobs SET body=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['id']))

    def save_job(self, job):
        with self.lock, self.conn:
            self.conn.execute('INSERT OR REPLACE INTO jobs(id,body) VALUES (?,?)', (job['id'], json.dumps(job, ensure_ascii=False)))

    def add_jobs(self, jobs):
        with self.lock, self.conn:
            for job in jobs:
                self.conn.execute('INSERT INTO jobs(id,body) VALUES (?,?)', (job['id'], json.dumps(job, ensure_ascii=False)))

    def get_job(self, job_id):
        with self.lock:
            row = self.conn.execute('SELECT body FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ServiceError('job_not_found', '작업을 찾지 못했습니다. 목록을 새로 확인하세요.')
            return json.loads(row[0])

    def list_jobs(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.conn.execute('SELECT body FROM jobs ORDER BY rowid DESC')]

    def reserve_call(self, limit, kind):
        with self.lock, self.conn:
            if limit is not None and self.calls_used() >= limit:
                raise ServiceError('model_budget', '모델 호출 누적 상한에 도달했습니다. 승인된 상한을 확인하세요.')
            self.conn.execute('INSERT INTO model_calls(kind, reserved_at) VALUES (?,?)', (kind, now()))

    def calls_used(self, kind=None):
        with self.lock:
            if kind is not None:
                return self.conn.execute('SELECT COUNT(*) FROM model_calls WHERE kind=?', (kind,)).fetchone()[0]
            return self.conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0]

    def close(self):
        with self.lock:
            self.conn.close()
