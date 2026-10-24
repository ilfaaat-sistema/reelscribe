from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.services import carousel_cover_store as store

import supabase


@pytest.fixture(autouse=True)
def clear_client_cache():
    store.get_cover_db.cache_clear()
    yield
    store.get_cover_db.cache_clear()


def test_service_role_missing_never_falls_back_to_anon(monkeypatch):
    monkeypatch.setattr(store, 'settings', SimpleNamespace(supabase_url='https://project.supabase.co',
                        supabase_service_role_key='', supabase_anon_key='fake-anon'))
    create = Mock()
    monkeypatch.setattr(supabase, 'create_client', create)
    with pytest.raises(RuntimeError):
        store.get_cover_db()
    create.assert_not_called()


def test_service_role_uses_separate_client_with_finite_timeout(monkeypatch):
    monkeypatch.setattr(store, 'settings', SimpleNamespace(supabase_url='https://project.supabase.co',
                        supabase_service_role_key='fake-service', supabase_anon_key='fake-anon'))
    created = object()
    create = Mock(return_value=created)
    monkeypatch.setattr(supabase, 'create_client', create)
    assert store.get_cover_db() is created
    assert store.get_cover_db() is created
    assert create.call_count == 1
    assert create.call_args.args == ('https://project.supabase.co', 'fake-service')
    assert create.call_args.kwargs['options'].postgrest_client_timeout == 10


@pytest.mark.parametrize('mode,status,retry_after', [
    ('pilot', 'available', None), ('pilot', 'limit_reached', None),
    ('live', 'available', None), ('live', 'limit_reached', '2030-01-01T21:00:00Z')])
@pytest.mark.parametrize('daily_limit', [500, 2500])
def test_budget_rpc_has_fixed_limits_and_utc_reset(monkeypatch, mode, status, retry_after, daily_limit):
    data = {'mode': mode, 'status': status, 'pilot_limit': 20, 'daily_limit': daily_limit,
            'reset_at': '2030-01-01T21:00:00Z', 'retry_after': retry_after}
    rpc = Mock(return_value=data)
    monkeypatch.setattr(store, '_rpc', rpc)
    assert store.CoverStore().budget() == data
    rpc.assert_called_once_with('carousel_cover_budget_state', {})


@pytest.mark.parametrize('changed', [
    {'mode': 'unknown'}, {'pilot_limit': 21}, {'daily_limit': 501},
    {'daily_limit': 2501}, {'daily_limit': True}, {'daily_limit': False},
    {'daily_limit': 500.0}, {'daily_limit': 2500.0}, {'daily_limit': '2500'},
    {'status': 'ready'}, {'reset_at': '2030-01-02T00:00:00+03:00'},
    {'retry_after': '2030-01-02T21:00:00Z'}, {'reset_at': 'invalid'},
    {'mode': 'pilot', 'retry_after': '2030-01-01T21:00:00Z'}])
def test_budget_rejects_inconsistent_or_raised_metadata(monkeypatch, changed):
    data = {'mode': 'live', 'status': 'limit_reached', 'pilot_limit': 20, 'daily_limit': 500,
            'reset_at': '2030-01-01T21:00:00Z', 'retry_after': '2030-01-01T21:00:00Z'}
    monkeypatch.setattr(store, '_rpc', Mock(return_value={**data, **changed}))
    with pytest.raises(ValueError):
        store.CoverStore().budget()



@pytest.mark.parametrize('rows', [[], [{'media_id':'1','media_url':'https://x.fbcdn.net/a'}]])
def test_source_override_read_is_id_scoped_and_excludes_expired(monkeypatch, rows):
    from datetime import datetime, timezone
    query = Mock()
    query.select.return_value = query
    query.eq.return_value = query
    query.gt.return_value = query
    query.limit.return_value = query
    query.execute.return_value = SimpleNamespace(data=rows)
    db = Mock()
    db.table.return_value = query
    monkeypatch.setattr(store, 'get_cover_db', Mock(return_value=db))
    before = datetime.now(timezone.utc)
    assert store.CoverStore().read_source_override('1') == (rows[0] if rows else None)
    db.table.assert_called_once_with('carousel_cover_source_overrides')
    query.eq.assert_called_once_with('media_id','1')
    assert query.gt.call_args.args[0] == 'expires_at'
    assert datetime.fromisoformat(query.gt.call_args.args[1]) >= before
    query.limit.assert_called_once_with(1)
