"""Проверки сценариев, зависящих от сохранённого снимка и версии записи."""
from app.services import carousel_selections as service


def row(media_id='123', removed_at=None, revision=3, likes=None):
    return {'instagram_media_id': media_id, 'selected_at': '2026-10-20T10:00:00Z',
            'revision': revision, 'removed_at': removed_at,
            'snapshot': {'id': media_id, 'username': 'alice', 'caption': 'Идея карусели',
                         'media_type': 'CAROUSEL_ALBUM', 'likes': likes, 'timestamp': '2026-10-19T10:00:00Z'},
            'source_period': '14d', 'source_start_date': '2026-10-06',
            'source_end_date': '2026-10-19', 'snapshot_at': '2026-10-20T10:00:00Z'}


def test_list_searches_snapshot_and_keeps_unknown_metrics_last(monkeypatch):
    monkeypatch.setattr(service, '_all_rows', lambda user: [row('1', likes=None), row('2', likes=20), row('3', likes=10)])
    result = service.list_selections('user', '', 'likes', 'desc', 50, 0)
    assert [item['id'] for item in result['items']] == ['2', '3', '1']
    assert result['selected_total'] == 3
    result = service.list_selections('user', 'ИДЕЯ', 'selected_at', 'asc', 50, 0)
    assert result['total'] == 3
    assert result['items'][0]['snapshot']['caption'] == 'Идея карусели'


def test_status_reports_removed_revision_for_undo(monkeypatch):
    monkeypatch.setattr(service, '_all_rows', lambda user: [row('1')])
    def db(method, path, **kwargs):
        assert kwargs['params']['instagram_media_id'] == 'in.(1,2,3)'
        return ([{'instagram_media_id': '1', 'revision': 3, 'removed_at': None},
                 {'instagram_media_id': '2', 'revision': 4, 'removed_at': '2026-10-20T11:00:00Z'}], {})
    monkeypatch.setattr(service, '_db', db)
    result = service.selection_status('user', ['1', '2', '3'])
    assert result == {'states': [
        {'id': '1', 'selected': True, 'revision': 3},
        {'id': '2', 'selected': False, 'revision': 4},
        {'id': '3', 'selected': False, 'revision': 0}], 'selected_total': 1}


def test_restore_uses_saved_snapshot_when_report_no_longer_contains_carousel(monkeypatch):
    saved = row(removed_at='2026-10-20T11:00:00Z')
    monkeypatch.setattr(service, '_row', lambda *args, **kwargs: saved)
    monkeypatch.setattr(service, '_report_item', lambda *args: None)
    def db(method, path, **kwargs):
        body = kwargs['payload']
        assert body['p_snapshot']['id'] == '123'
        assert body['p_expected_revision'] == 3
        assert body['p_start_date'] == '2026-10-06'
        return ({'item': {'id': '123', 'selected': True, 'revision': 4, 'snapshot': body['p_snapshot']},
                 'selected_total': 1}, {})
    monkeypatch.setattr(service, '_db', db)
    result = service.select_carousel('user', '123', '14d', 3)
    assert result['item']['snapshot']['caption'] == 'Идея карусели'
