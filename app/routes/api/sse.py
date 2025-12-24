"""SSE (Server-Sent Events) and related debug endpoints."""
import json
from flask import Response, stream_with_context, jsonify
from app.routes.api import bp, api_auth_required


@bp.route('/jobs/<int:job_id>/events')
@api_auth_required
def job_events(job_id):
    """SSE endpoint for job events. Streams JSON messages of the form:
    {"type": "log" | "status" | "metrics", "data": {...}}
    """
    try:
        from app.sse import register_event_listener, unregister_event_listener
    except Exception:
        return jsonify({'error': 'SSE not available in this deployment'}), 501

    def gen():
        q = register_event_listener(job_id)
        try:
            # Send an initial JSON 'connected' event so the client receives an onmessage and
            # clears its idle watchdog (previously only a comment was sent, which doesn't
            # trigger onmessage).
            yield 'data: ' + json.dumps({'type': 'connected', 'data': {}}) + '\n\n'
            while True:
                try:
                    msg = q.get(timeout=15)
                except Exception:
                    # keepalive comment to keep connection alive
                    yield ': keepalive\n\n'
                    continue
                # Send as raw data (client will JSON-parse)
                yield f"data: {msg}\n\n"
        finally:
            unregister_event_listener(job_id, q)

    return Response(stream_with_context(gen()), mimetype='text/event-stream')


@bp.route('/_debug/sse')
@api_auth_required
def debug_sse():
    try:
        from app.sse import get_status
        return jsonify({'sse': get_status()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
