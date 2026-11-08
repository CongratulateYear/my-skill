"""Small credential-free orchestration guards; no business state or remote requests."""
from __future__ import annotations
import json
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bridge_precheck(url=None):
    """Only /ping on the existing local ERP bridge. Never starts a bridge/session."""
    base = url or 'http://127.0.0.1:8901'
    try:
        parsed = urlsplit(base)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/') or not parsed.port):
            return {'status': 'skipped_nonlocal_or_invalid', 'login_state': 'unverified'}
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(base.rstrip('/') + '/ping'), timeout=1) as response:
            content = response.read(4097)
        if len(content) > 4096:
            raise ValueError('oversized')
        state = json.loads(content)
        if state.get('ok') is not True or type(state.get('agentAttached')) is not bool:
            raise ValueError('invalid')
        return {'status': 'attached' if state['agentAttached'] else 'agent_missing',
                'login_state': 'unverified'}
    except Exception:
        # Network/URL/body text and identity must not leak from advisory diagnostics.
        return {'status': 'unavailable', 'login_state': 'unverified'}


def wait_decision(*, job_id, last_read_job=None, event=None, diagnostic=False,
                  event_consumed=False):
    """Pure agent-side guard, including continuation rounds; not a job scheduler.

    Keep last_read_job in session task context after a read. Auto continuation and
    heartbeats do not authorize another read. Host completion/failure or an
    explicit diagnostic reason can. Never infer completion from elapsed time.
    A consumed host event stays consumed across automatic continuation rounds;
    callers retain this bit in session context, not in the business journal.
    """
    if not job_id:
        return 'no_job'
    if (event in ('completed', 'failed') and not event_consumed) or diagnostic:
        return 'collect_once'
    if last_read_job == job_id:
        return 'yield_until_host_event'
    return 'await_host_notification'


def input_wait_decision(*, waiting_key, last_prompt_key=None, new_input=False,
                        new_session_evidence=False):
    """Session-only reminder deduplication, not a scheduler or authorization.

    A stable key identifies company/batch/job plus missing item; never put secret
    values in it. Only genuinely new input/session evidence warrants reinspection.
    """
    if not waiting_key:
        return 'not_waiting'
    if new_input or new_session_evidence:
        return 'inspect_original_stop_point'
    if waiting_key == last_prompt_key:
        return 'yield_until_new_input'
    return 'ask_once'
