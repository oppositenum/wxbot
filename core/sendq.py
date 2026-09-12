"""发送队列：单 worker、FIFO 串行处理发送任务。

网页/接口把发送任务入队后立即拿到 job id 返回，不阻塞界面；worker 一条一条发，
新任务排在正在发的那条【后面】，绝不打断/穿插正在执行的发送。
(sender 里另有 SEND_LOCK 保证与机器人线程的发送也彼此串行。)
"""
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import account_session as sessions, send_ledger
from core import sender  # noqa: E402

_q = queue.Queue()
_jobs = {}                 # job_id -> {status, position, ...result}
_seq = [0]
_lock = threading.Lock()
_worker = [None]
_MAX_JOBS = 60


def _trim():
    if len(_jobs) > _MAX_JOBS:
        for k in sorted(_jobs, key=lambda x: x)[:len(_jobs) - _MAX_JOBS]:
            _jobs.pop(k, None)


def _run():
    while True:
        item = _q.get()
        jid = item["id"]
        _jobs[jid] = {"status": "running", "to": item["to"]}
        t0 = time.time()
        try:
            if item["kind"] == "text":
                res = sender.send_text(item["to"], item["content"],
                                       chat_username=item["chat"], job_id=jid, session=item["session"])
            else:
                res = sender.send_image(item["to"], item["path"],
                                        chat_username=item["chat"], job_id=jid, session=item["session"])
        except Exception as e:  # noqa: BLE001
            res = send_ledger.result(jid) or {"ok": False, "status": "not_sent",
                "reason": "queue_exception_" + type(e).__name__, "retryable": False}
        _jobs[jid] = {"status": "done" if res.get("status") in ("confirmed", "submitted") else res.get("status", "uncertain"),
                      "to": item["to"], "elapsed": round(time.time() - t0, 1), **res}
        if res.get('retryable'):
            delay = max(1, res.get('retry_at', 0) - time.time())
            timer = threading.Timer(delay, _q.put, args=(item,))
            timer.daemon = True
            timer.start()
        # 入队后 worker 每完成一条，刷新其余排队任务的位置
        _reposition()
        _trim()
        _q.task_done()


def _reposition():
    pending = [k for k in _jobs if _jobs[k].get("status") == "queued"]
    for i, k in enumerate(sorted(pending, key=lambda x: x), start=1):
        _jobs[k]["position"] = i


def _ensure_worker():
    with _lock:
        if _worker[0] is None or not _worker[0].is_alive():
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            _worker[0] = t


def enqueue(kind, to, chat=None, content=None, path=None):
    """入队一条发送任务，返回 (job_id, 前面还有几条在排队)。"""
    token = sessions.capture()
    sessions.check(token)
    _ensure_worker()
    with _lock:
        _seq[0] += 1
        jid = send_ledger.next_id()
    ahead = _q.qsize() + (1 if any(v.get("status") == "running"
                                   for v in _jobs.values()) else 0)
    send_ledger.Ledger().prepare(jid, token, chat, kind,
        send_ledger.stable_id(content if kind == 'text' else path),
        content if kind == 'text' else path)
    _jobs[jid] = {"status": "queued", "position": _q.qsize() + 1, "to": to}
    _q.put({"id": jid, "kind": kind, "to": to, "chat": chat,
            "content": content, "path": path, "session": token})
    return jid, ahead


def status(job_id):
    result = dict(_jobs.get(job_id) or send_ledger.result(job_id) or {'status': 'unknown'})
    outcome = result['status']
    result['send_status'] = outcome
    if outcome in ('confirmed', 'submitted'):
        result['status'] = 'done'
    elif outcome in ('uncertain', 'stale', 'failed', 'not_sent'):
        result['status'] = 'error'  # compatibility with the existing web poller
        labels = {'uncertain': '发送结果待核对，请勿重复发送',
                  'stale': '账号已变化，本次任务已保留待核对',
                  'failed': '本次发送明确失败', 'not_sent': '本次未执行发送'}
        result['error'] = labels[outcome] + '：' + result.get('reason', '')
        if result.get('reason') == 'cannot_confirm_target':
            result['error'] = '无法确认目标，本次未执行发送'
    return result


def stats():
    return {"queued": _q.qsize(),
            "running": sum(1 for v in _jobs.values() if v.get("status") == "running")}
