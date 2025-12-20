"""Simple in-memory SSE/pubsub utilities for job events.

This module provides thread-safe registration of listeners (queues) per job id,
and helper to send JSON-serializable events to all listeners of a job.

Note: This is an in-memory mechanism intended for single-node deployments or
for streams handled by the same worker running the job. It's not intended as a
cross-worker message bus. For multi-worker setups you should implement a
central pub/sub (Redis, etc.) if needed.
"""
from collections import defaultdict
import json
import threading
import queue
import os

_listeners = defaultdict(list)  # job_id -> list of Queue
_lock = threading.Lock()

# Optional Redis support (for multi-worker deployments)
_redis_client = None
_redis_subscribers = {}  # job_id -> {'thread': Thread, 'stop': Event}
_use_redis = False

REDIS_URL = os.environ.get('REDIS_URL')
if REDIS_URL:
    try:
        import redis
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        _use_redis = True
    except Exception:
        # If redis package not available or connection fails, fall back to in-memory
        _redis_client = None
        _use_redis = False


def _start_redis_subscriber(job_id):
    """Start a background thread that subscribes to Redis channel for job_id and
    forwards messages into local in-memory listener queues.
    """
    if not _use_redis or job_id in _redis_subscribers:
        return

    stop_event = threading.Event()

    def run():
        try:
            pubsub = _redis_client.pubsub(ignore_subscribe_messages=True)
            channel = f"job-events:{job_id}"
            pubsub.subscribe(channel)
            while not stop_event.is_set():
                msg = pubsub.get_message(timeout=1)
                if msg and msg.get('data'):
                    data = msg['data']
                    # Forward to local queues
                    with _lock:
                        queues = list(_listeners.get(job_id, []))
                    for q in queues:
                        try:
                            q.put_nowait(data)
                        except Exception:
                            pass
        except Exception:
            # If anything goes wrong, just stop subscriber
            pass

    t = threading.Thread(target=run, daemon=True)
    _redis_subscribers[job_id] = {'thread': t, 'stop': stop_event}
    t.start()


def _stop_redis_subscriber(job_id):
    s = _redis_subscribers.get(job_id)
    if not s:
        return
    try:
        s['stop'].set()
    except Exception:
        pass
    try:
        s['thread'].join(timeout=2)
    except Exception:
        pass
    with _lock:
        try:
            del _redis_subscribers[job_id]
        except Exception:
            pass


def register_event_listener(job_id):
    q = queue.Queue()
    with _lock:
        _listeners[job_id].append(q)

    # If using Redis, ensure a subscriber thread is running for this job_id
    if _use_redis:
        _start_redis_subscriber(job_id)
    return q


def unregister_event_listener(job_id, q):
    with _lock:
        lst = _listeners.get(job_id)
        if not lst:
            return
        try:
            lst.remove(q)
        except ValueError:
            return
        if not lst:
            del _listeners[job_id]
            # Stop redis subscriber if present
            if _use_redis:
                _stop_redis_subscriber(job_id)


def send_event(job_id, event_type, payload):
    """Send a JSON event to all registered listeners for job_id.

    payload should be JSON-serializable. The function will attempt to put the
    serialized message into each listener queue (non-blocking). If Redis is
    configured, the event will also be published to the channel `job-events:<id>`
    so other workers can receive it.
    """
    data = json.dumps({'type': event_type, 'data': payload}, default=str)

    # Local in-memory delivery
    with _lock:
        queues = list(_listeners.get(job_id, []))
    for q in queues:
        try:
            q.put_nowait(data)
        except Exception:
            # best-effort; drop if queue is full or closed
            pass

    # Publish to Redis if enabled (best-effort)
    if _use_redis and _redis_client:
        try:
            channel = f"job-events:{job_id}"
            _redis_client.publish(channel, data)
        except Exception:
            pass