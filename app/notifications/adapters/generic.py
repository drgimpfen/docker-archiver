from typing import List, Optional, Tuple


def _make_apobj(urls: Optional[List[str]] = None) -> Tuple[Optional[object], int, Optional[str]]:
    try:
        import apprise
    except Exception as e:
        return None, 0, f'apprise not available: {e}'

    apobj = apprise.Apprise()
    added = 0
    for u in (urls or []):
        try:
            ok = apobj.add(u)
            if ok:
                added += 1
        except Exception:
            pass
    return apobj, added, None


def _notify_with_retry(apobj: object, title: str, body: str, body_format: object = None, attach: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    try:
        res = apobj.notify(title=title, body=body, body_format=body_format, attach=attach)
        return bool(res), None
    except Exception as e:
        # Log the full exception for improved diagnostics and attempt one retry
        import traceback, time
        traceback_str = traceback.format_exc()
        try:
            time.sleep(0.5)
            res = apobj.notify(title=title, body=body, body_format=body_format, attach=attach)
            return bool(res), None
        except Exception as re:
            # Include the original traceback and retry exception in the returned detail
            retry_tb = traceback.format_exc()
            detail = f"first: {traceback_str.strip()} | retry: {retry_tb.strip()}"
            return False, detail


from .base import AdapterBase, AdapterResult


class GenericAdapter(AdapterBase):
    """Send to configured Apprise URLs only (generic transport adapter)."""

    def __init__(self, urls: Optional[List[str]] = None):
        self.urls = list(urls or [])

    def send(self, title: str, body: str, body_format: object = None, attach: Optional[str] = None, context: str = '') -> AdapterResult:
        apobj, added, err = _make_apobj(self.urls)
        if apobj is None:
            return AdapterResult(channel='generic', success=False, detail=err)
        if added == 0:
            return AdapterResult(channel='generic', success=False, detail='no apprise URLs added')

        ok, detail = _notify_with_retry(apobj, title=title, body=body, body_format=body_format, attach=attach)
        if ok:
            return AdapterResult(channel='generic', success=True)
        return AdapterResult(channel='generic', success=False, detail=f'notify exception: {detail}')