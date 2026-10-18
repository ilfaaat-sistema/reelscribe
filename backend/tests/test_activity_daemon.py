from datetime import datetime, timedelta, timezone

import pytest

from app.workers.author_activity_daemon import run_session


class Clock:
    def __init__(self):
        self.seconds = 0
        self.base = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.waits = []

    def now(self):
        return self.base + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.seconds += seconds


def report(status, checked, requests, **extra):
    return {'collection': dict(status=status, checked=checked, target=1600, requests=requests, **extra),
            'totals': {'carousels': 10}}


def test_keeps_collecting_even_when_all_profiles_seen_but_last_page_pending():
    queue = [report('running', 1600, 1, pending=0), report('finished', 1600, 2, pending=0)]
    stored, dispatched = [], []
    result = run_session(collect=lambda **kwargs: queue.pop(0), persist=stored.append,
                         requeue=lambda: dispatched.append(True), clock=Clock(), log=lambda _: None)
    assert result == 'finished'
    assert [r['collection']['requests'] for r in stored] == [1, 2]
    assert not dispatched


def test_cooldown_is_waited_in_same_runner_without_cron():
    clock = Clock()
    until = (clock.now() + timedelta(minutes=10)).isoformat()
    queue = [report('cooldown', 100, 20, cooldown_until=until), report('finished', 1600, 40)]
    calls, dispatched = [], []

    def collect(**limits):
        calls.append(clock.now())
        return queue.pop(0)

    assert run_session(collect=collect, persist=lambda _: None, requeue=lambda: dispatched.append(True),
                       clock=clock, log=lambda _: None) == 'finished'
    assert (calls[1] - calls[0]).total_seconds() == 600
    assert max(clock.waits) <= 45
    assert not dispatched


def test_session_end_dispatches_continuation_once_during_long_cooldown():
    clock, dispatched = Clock(), []
    r = report('cooldown', 152, 124, cooldown_until=(clock.now() + timedelta(hours=1)).isoformat())
    result = run_session(collect=lambda **_: r, persist=lambda _: None,
                         requeue=lambda: dispatched.append(True), clock=clock, max_seconds=60, log=lambda _: None)
    assert result == 'continued'
    assert dispatched == [True]
    assert clock.seconds < 60


def test_authentication_failure_does_not_make_unbounded_dispatch_loop():
    dispatched = []
    result = run_session(collect=lambda **_: report('authentication_required', 152, 124),
                         persist=lambda _: None, requeue=lambda: dispatched.append(True), clock=Clock(), log=lambda _: None)
    assert result == 'authentication_required'
    assert not dispatched


def test_publication_failure_retries_then_hands_off_existing_cloud_checkpoint():
    attempts, dispatched = [], []

    def persist(_):
        attempts.append(True)
        raise ConnectionError('Проверочная ошибка GitHub')

    result = run_session(collect=lambda **_: report('running', 152, 124), persist=persist,
                         requeue=lambda: dispatched.append(True), clock=Clock(), log=lambda _: None)
    assert len(attempts) == 3
    assert dispatched == [True]
    assert result == 'continued'


def test_no_progress_cannot_spin_forever():
    with pytest.raises(RuntimeError, match='не продвигается'):
        run_session(collect=lambda **_: report('running', 152, 124), persist=lambda _: None,
                    requeue=lambda: None, clock=Clock(), log=lambda _: None)


def test_permanent_store_failure_does_not_dispatch_poisoned_chain():
    dispatched = []
    def persist(_):
        raise ValueError('Контрольная точка другого назначения')
    with pytest.raises(ValueError):
        run_session(collect=lambda **_: report('running', 152, 124), persist=persist,
                    requeue=lambda: dispatched.append(True), clock=Clock(), log=lambda _: None)
    assert not dispatched


def test_transient_dispatch_is_retried():
    attempts = []
    def dispatch():
        attempts.append(True)
        if len(attempts) < 3:
            raise ConnectionError('Временная сеть')
    clock = Clock()
    r = report('cooldown', 152, 124, cooldown_until=(clock.now() + timedelta(hours=1)).isoformat())
    assert run_session(collect=lambda **_: r, persist=lambda _: None, requeue=dispatch,
                       clock=clock, max_seconds=60, log=lambda _: None) == 'continued'
    assert len(attempts) == 3


@pytest.mark.parametrize('error', [FileNotFoundError('Нет seed'), PermissionError('Нет записи')])
def test_filesystem_errors_never_dispatch_another_runner(error):
    dispatched = []
    def persist(_):
        raise error
    with pytest.raises(type(error)):
        run_session(collect=lambda **_: report('running', 152, 124), persist=persist,
                    requeue=lambda: dispatched.append(True), clock=Clock(), log=lambda _: None)
    assert not dispatched


def test_scheduled_and_continued_runs_skip_permanent_errors_and_finished():
    from app.workers.author_activity_daemon import should_skip
    for status in ('authentication_required', 'permissions_or_request_error', 'failed'):
        assert should_skip(status)
        assert not should_skip(status, retry_blocked=True)
    assert should_skip('finished', retry_blocked=True)
    assert not should_skip('running')
    assert not should_skip('cooldown')
