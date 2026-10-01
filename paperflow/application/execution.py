"""Cross-worker, process-shared project leases and actual loop model reservations."""
from __future__ import annotations
import json
import time
from paperflow.engine.journals.models import JournalError

class ExecutionCoordinator:
    def __init__(self, store):
        self.store=store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS execution_leases (project_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS model_reservations (ticket TEXT PRIMARY KEY, project_id TEXT NOT NULL, action_id TEXT NOT NULL, run_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS run_loop_engagement (run_id TEXT NOT NULL, project_id TEXT NOT NULL, PRIMARY KEY(run_id,project_id))')
            db.execute('CREATE TABLE IF NOT EXISTS run_scopes (run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, snapshot TEXT NOT NULL)')

    def recover(self,run_ids):
        reports=[]
        for run_id in run_ids:
            pending=len(self.pending(run_id))
            self.release('agent:'+run_id)
            reports.append({'run_id':run_id,'lease_released':True,'pending_model_reservations':pending,'needs_reconciliation':bool(pending)})
        return reports

    def scope(self,run_id):
        with self.store.connect() as db:
            row=db.execute('SELECT snapshot FROM run_scopes WHERE run_id=?',(run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_scope(self,run_id,workspace_id,snapshot):
        with self.store.connect() as db:
            db.execute('INSERT INTO run_scopes VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET snapshot=excluded.snapshot',(run_id,workspace_id,json.dumps(snapshot,ensure_ascii=False,sort_keys=True)))

    def acquire(self,project_id,owner,ttl=3600):
        if not isinstance(owner,str) or not owner or len(owner)>200 or type(ttl) is not int or not 1<=ttl<=3600:
            raise JournalError('INVALID_INPUT','执行权参数无效')
        now=time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            unresolved=db.execute("SELECT run_id FROM model_reservations WHERE project_id=? AND state='pending'",(project_id,)).fetchall()
            if any('agent:'+row[0]!=owner for row in unresolved):
                raise JournalError('NEEDS_RECONCILIATION','该项目有未结算模型调用；请先核对实际请求')
            current=db.execute('SELECT owner,expires_at FROM execution_leases WHERE project_id=?',(project_id,)).fetchone()
            if current and current['expires_at']>now and current['owner']!=owner:
                raise JournalError('EXECUTION_BUSY','项目正在由另一个 GUI/Agent 执行器运行')
            db.execute('INSERT INTO execution_leases VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at',(project_id,owner,now+ttl))
        return {'project_id':project_id,'owner':owner,'expires_at':now+ttl}

    def ensure(self,project_id,owner):
        with self.store.connect() as db:
            row=db.execute('SELECT owner,expires_at FROM execution_leases WHERE project_id=?',(project_id,)).fetchone()
        if not row or row['owner']!=owner or row['expires_at']<=time.time():
            raise JournalError('EXECUTION_LOST','执行权已丢失，不能重新抢占')
        return self.acquire(project_id,owner)

    def engage(self,run_id,project_id):
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO run_loop_engagement VALUES (?,?)',(run_id,project_id))

    def engaged(self,run_id):
        with self.store.connect() as db:
            return [row[0] for row in db.execute('SELECT project_id FROM run_loop_engagement WHERE run_id=? ORDER BY project_id',(run_id,))]

    def check(self,project_id,owner=None):
        with self.store.connect() as db:
            current=db.execute('SELECT owner,expires_at FROM execution_leases WHERE project_id=?',(project_id,)).fetchone()
            unresolved=db.execute("SELECT run_id FROM model_reservations WHERE project_id=? AND state='pending'",(project_id,)).fetchall()
        if any('agent:'+row[0]!=owner for row in unresolved):
            raise JournalError('NEEDS_RECONCILIATION','项目有未结算的模型调用')
        if current and current['expires_at']>time.time() and current['owner']!=owner:
            raise JournalError('EXECUTION_BUSY','项目正在由其他执行器修改')

    def release(self,owner):
        with self.store.connect() as db:
            db.execute('DELETE FROM execution_leases WHERE owner=?',(owner,))
            if owner.startswith('agent:'):
                db.execute('DELETE FROM run_loop_engagement WHERE run_id=?',(owner[6:],))

    def pending(self,run_id):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM model_reservations WHERE run_id=? AND state='pending'",(run_id,))]

    def reserve(self,run_id,result,action_id):
        data=result['data']
        with self.store.connect() as db:
            db.execute('INSERT INTO model_reservations VALUES (?,?,?,?,?,?)',(data['model_ticket'],data['project_id'],action_id,run_id,'pending',json.dumps({'project_revision':data['project_revision'],'loop_revision':data['loop_revision']},ensure_ascii=False)))

    def settled(self,ticket,result):
        with self.store.connect() as db:
            db.execute("UPDATE model_reservations SET state='settled',payload=? WHERE ticket=? AND state='pending'",(json.dumps(result,ensure_ascii=False),ticket))

    def release_unissued(self,ticket,result):
        with self.store.connect() as db:
            db.execute("UPDATE model_reservations SET state='unissued_released',payload=? WHERE ticket=? AND state='pending'",(json.dumps(result,ensure_ascii=False),ticket))
