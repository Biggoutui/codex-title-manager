"""Apply user-reviewed title plans using native Codex APIs; no database writes."""
import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
FIELDS = ("id", "projectId", "createdAt", "updatedAt", "recencyAt")
SAFE_STATUS = {"notLoaded", "idle"}

class SafetyStop(RuntimeError):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

def atomic_save(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

@contextmanager
def locked(path):
    # OS lock is released after crashes; the empty lock file can safely remain.
    lock_path = Path(str(path) + ".lock")
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SafetyStop("Another execution owns the ledger lock") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SafetyStop("Another execution owns the ledger lock") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)

def load_plan(path):
    plan = json.loads(Path(path).read_text(encoding="utf8"))
    rows = plan["renames"]
    if not rows or len({r["threadId"] for r in rows}) != len(rows):
        raise SafetyStop("Plan must contain unique nonempty entries")
    keys = ("threadId", "old_title", "proposed_title")
    clean = []
    for row in rows:
        r = {k: row[k] for k in keys}
        if any(not isinstance(r[k], str) or not r[k] for k in keys):
            raise SafetyStop("Invalid reviewed entry")
        if r["old_title"] == r["proposed_title"]:
            raise SafetyStop("Unchanged title in execution list")
        clean.append(r)
    digest = hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True).encode("utf8")).hexdigest()
    return clean, digest

def transition(data, row, path, state, reason=None, **extra):
    row.update(extra)
    row["state"] = state
    row["events"].append({"at": now(), "state": state, **({"reason": reason} if reason else {})})
    atomic_save(path, data)

def read_thread(rpc, tid):
    t = rpc.request("thread/read", {"threadId": tid, "includeTurns": False})["thread"]
    if not isinstance(t, dict):
        raise SafetyStop("Invalid thread metadata")
    return t

def snapshot(thread):
    if any(k not in thread for k in FIELDS):
        raise SafetyStop("Required identity/project/recency metadata is missing")
    return {k: thread[k] for k in FIELDS}

def unarchived_ids(rpc):
    ids, cursors, cursor = {}, set(), None
    while True:
        params = {"archived": False, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        result = rpc.request("thread/list", params)
        ids.update((t["id"], t) for t in result["data"])
        cursor = result.get("nextCursor")
        if not cursor:
            return ids
        if cursor in cursors:
            raise SafetyStop("Repeated inventory cursor")
        cursors.add(cursor)

def reconcile(data, row, path, rpc):
    restoring = row["state"] == "restore_pending"
    expected = row["old_title"] if restoring else row["proposed_title"]
    original = row["proposed_title"] if restoring else row.get("original_native_name", row["old_title"])
    try:
        t = read_thread(rpc, row["threadId"])
        current = snapshot(t)
    except Exception as exc:
        transition(data, row, path, row["state"], "readback_unavailable", error_type=type(exc).__name__)
        raise SafetyStop("Write outcome unknown; rerun to reconcile before further writes") from None
    if current != row["before"]:
        transition(data, row, path, "invariant_failed", "identity_project_or_recency_changed", after=current,
                   observed_target=t.get("name") == expected, owned_write=t.get("name") == expected,
                   operation="restore" if restoring else "rename")
        raise SafetyStop("Metadata invariant failed; inspect ledger before further writes")
    if t.get("name") == expected:
        transition(data, row, path, "restored" if restoring else "applied", after=current,
                   owned_write=not restoring)
    elif t.get("name") == original:
        transition(data, row, path, "restore_failed" if restoring else "not_applied", "write_not_observed")
        raise SafetyStop("Write was not observed; no automatic retry")
    else:
        transition(data, row, path, "restore_conflict" if restoring else "conflict", "title_changed_after_pending")
        raise SafetyStop("Concurrent title conflict; no further writes")

def change(data, row, path, rpc, visible, restoring=False, excluded=()):
    tid = row["threadId"]
    state = "restore_skipped" if restoring else "skipped"
    if tid in excluded or tid not in visible:
        transition(data, row, path, state, "current_thread_or_archived_or_unlisted")
        return
    try:
        t = read_thread(rpc, tid)
        before = snapshot(t)
        if t["id"] != tid:
            raise SafetyStop("Thread identity mismatch")
        status = t.get("status")
        status = status.get("type") if isinstance(status, dict) else status
        if status not in SAFE_STATUS:
            transition(data, row, path, state, "running_or_unknown_status")
            return
        if restoring and before != row.get("after"):
            transition(data, row, path, state, "metadata_changed_since_rename")
            return
        expected = row["proposed_title"] if restoring else row["old_title"]
        if t.get("name") != expected:
            transition(data, row, path, state, "title_conflict")
            return
    except Exception as exc:
        transition(data, row, path, state, "preflight_unavailable", error_type=type(exc).__name__)
        return
    pending = "restore_pending" if restoring else "pending"
    target = row["old_title"] if restoring else row["proposed_title"]
    transition(data, row, path, pending, before=before)
    try:
        rpc.request("thread/name/set", {"threadId": tid, "name": target})
    except Exception as exc:
        transition(data, row, path, pending, "rpc_write_uncertain", error_type=type(exc).__name__)
    reconcile(data, row, path, rpc)

def execute(command, plan_path, ledger_path, rpc=None, batch_size=3, excluded=()):
    ledger_path = Path(ledger_path)
    rows, digest = load_plan(plan_path)
    if not 1 <= batch_size <= 10: raise SafetyStop('Batch size must be 1 to 10')
    if command == 'status':
        if not ledger_path.exists(): return {'total':len(rows),'states':{'ready':len(rows)}}
        data=json.loads(ledger_path.read_text(encoding='utf8'))
        if data['plan_sha256']!=digest: raise SafetyStop('Plan changed')
        return {'total':len(rows),'states':dict(Counter(r['state'] for r in data['entries']))}
    if command not in {'preview','apply','restore'} or rpc is None: raise SafetyStop('Invalid command or unavailable RPC')
    if command=='preview':
        visible=unarchived_ids(rpc); eligible=0; skipped=0
        for row in rows:
            try:
                t=read_thread(rpc,row['threadId']); snapshot(t)
                safe=t.get('status',{}).get('type') in SAFE_STATUS
                match=t.get('id')==row['threadId'] and t.get('name')==row['old_title']
                ok=row['threadId'] in visible and row['threadId'] not in excluded and safe and match
                eligible+=int(ok); skipped+=int(not ok)
            except Exception: skipped+=1
        return {'total':len(rows),'eligible':eligible,'skipped':skipped,'writes':0}
    ledger_path.parent.mkdir(parents=True,exist_ok=True)
    with locked(ledger_path):
        if ledger_path.exists():
            data=json.loads(ledger_path.read_text(encoding='utf8'))
            if data['plan_sha256']!=digest or [{k:r[k] for k in rows[0]} for r in data['entries']]!=rows:
                raise SafetyStop('Reviewed plan or ledger changed')
        else:
            if command=='restore': return {'restored':0,'reason':'no ledger'}
            data={'version':1,'created_at':now(),'plan_sha256':digest,'entries':[dict(r,state='ready',events=[]) for r in rows]}
            atomic_save(ledger_path,data)
        for row in data['entries']:
            if row['state'] in {'pending','restore_pending'}: reconcile(data,row,ledger_path,rpc)
        if command!='restore' and any(r['state'] in {'invariant_failed','conflict','not_applied','restore_failed','restore_conflict'} for r in data['entries']):
            raise SafetyStop('Unresolved failure; inspect private ledger or restore')
        if command=='apply': selected=[r for r in data['entries'] if r['state']=='ready'][:batch_size]
        else: selected=[r for r in reversed(data['entries']) if r.get('owned_write') and r['state'] not in {'restored','restore_skipped'}]
        if selected:
            visible=unarchived_ids(rpc)
            for row in selected: change(data,row,ledger_path,rpc,visible,restoring=command=='restore',excluded=excluded)
        return {'total':len(rows),'states':dict(Counter(r['state'] for r in data['entries']))}

class Rpc:
    def __init__(self):
        import catalog_repair as catalog
        self.native=catalog.NativeConfig(catalog.active_cli(),catalog.CONFIG)
    def request(self,method,params): return self.native.call(method,params)
    def close(self): self.native.close()

def main():
    import catalog_repair as catalog
    parser=argparse.ArgumentParser(description='Review a user-supplied title plan, then apply small reversible batches.')
    parser.add_argument('--exe')
    parser.add_argument('--codex-home')
    parser.add_argument('--data-dir')
    parser.add_argument('--batch-size',type=int,default=3)
    parser.add_argument('--exclude',action='append',default=[],help='Exclude a current/running thread ID; repeatable')
    parser.add_argument('--yes',action='store_true',help='Required for apply or restore')
    parser.add_argument('command',choices=['preview','apply','status','restore'])
    parser.add_argument('plan',type=Path,help='Private JSON plan; do not commit real plans')
    args=parser.parse_args()
    catalog.configure(args.exe,args.codex_home,args.data_dir)
    rpc=None
    try:
        if args.command in {'apply','restore'} and not args.yes: raise SafetyStop('Review preview first; pass --yes to mutate')
        if args.command!='status': rpc=Rpc()
        if args.command in {'apply','restore'}: catalog.ensure_data_dir()
        # Each plan has its own ledger; modified plans are rejected by the ledger hash.
        plan_key=hashlib.sha256(str(args.plan.expanduser().resolve()).encode('utf8')).hexdigest()[:24]
        ledger=catalog.ROOT/('history-'+plan_key+'.json')
        result=execute(args.command,args.plan,ledger,rpc,args.batch_size,args.exclude)
        print(json.dumps(result,ensure_ascii=True)); return 0
    except Exception as exc:
        print(json.dumps({'stopped':True,'error_type':type(exc).__name__,
                          'message':str(exc) if isinstance(exc,SafetyStop) else 'Inspect private ledger; RPC details omitted'})); return 1
    finally:
        if rpc: rpc.close()

if __name__=='__main__': sys.exit(main())
