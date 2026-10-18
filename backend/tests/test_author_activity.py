"""Проверки дат, покрытия и дедупликации без внешних интеграций."""
from copy import deepcopy
from datetime import datetime

import pytest

from app.radar.activity import build_activity_report, normalize_activity_item


def item(id='1', **kwargs):
    return dict(id=id, ownerUsername='alice', timestamp='2026-10-15T10:00:00Z', type='Image', **kwargs)


def full(*users):
    return {u: {'posts': 'complete', 'reels': 'complete'} for u in users}


def test_timezone_boundaries_pinned_and_today():
    dates = ['2026-10-14T20:59:59Z', '2026-10-14T21:00:00Z',
             '2026-10-17T20:59:59Z', '2026-10-17T21:00:00Z', '2024-01-01T10:00:00Z']
    data = [dict(item(str(i)), timestamp=d) for i, d in enumerate(dates)]
    report = build_activity_report(['alice'], data, [], full('alice'))
    assert [d['observed_total'] for d in report['days']] == [1, 0, 1]
    assert report['totals']['exact_total'] == 2
    assert report['totals']['avg_per_day'] == 2 / 3


def test_epoch_and_carousel_parent_only():
    stamp = datetime.fromisoformat('2026-10-14T21:00:00+00:00').timestamp()
    data = [dict(item(), timestamp=stamp, type='Sidecar', childPosts=[item('child1'), item('child2')]),
            item('child1', parentId='1')]
    report = build_activity_report(['alice'], data, [], full('alice'))
    assert report['totals']['observed_total'] == 1
    assert report['totals']['carousels'] == 1
    assert report['diagnostics'][0]['reason'] == 'carousel_child'


def test_union_identifiers_bridge_and_reel_precedence():
    data = [item('10'), dict(item(None), shortCode='ABC'), item('10', shortCode='ABC')]
    report = build_activity_report(['alice'], data, [dict(item(None), shortCode='ABC')], full('alice'))
    assert report['totals']['observed_total'] == 1
    assert report['totals']['reels'] == 1
    assert report['totals']['unique_content']['observed_total'] == 1


def test_collaboration_counts_publications_and_unique_content_separately():
    data = [item('10', inputUrl='https://www.instagram.com/alice/'),
            item('10', inputUrl='https://www.instagram.com/bob/')]
    report = build_activity_report(['alice', 'bob'], data, [], full('alice', 'bob'))
    assert [p['exact_total'] for p in report['profiles']] == [1, 1]
    assert report['totals']['exact_total'] == 2
    assert report['totals']['unique_content']['observed_total'] == 1


def test_empty_complete_profiles_have_three_exact_zero_days():
    report = build_activity_report(['alice'], [], [], full('alice'))
    assert [d['exact_total'] for d in report['profiles'][0]['days']] == [0, 0, 0]
    assert report['totals']['avg_per_day'] == 0


@pytest.mark.parametrize('status', ['partial', 'error', 'not_checked', 'unexpected', None])
def test_incomplete_coverage_never_claims_exact_zero(status):
    coverage = {'alice': {'posts': 'complete', 'reels': status}}
    report = build_activity_report(['alice'], [item()], [], coverage)
    assert report['totals']['observed_total'] == 1
    assert report['totals']['exact_total'] is None
    assert report['totals']['avg_per_day'] is None
    assert report['totals']['observed_avg_per_day'] == 1 / 3
    assert report['days'][1]['exact_total'] is None
    assert report['profiles'][0]['days'][1]['exact_total'] is None


def test_absent_coverage_is_unknown_and_does_not_mutate_input():
    data, cov = [item()], {}
    before = deepcopy(data)
    report = build_activity_report(['alice'], data, [], cov)
    assert report['profiles'][0]['coverage'] == {'posts': 'not_checked', 'reels': 'not_checked'}
    assert data == before and cov == {}
    assert report['totals']['exact_total'] is None


@pytest.mark.parametrize('stamp', [None, 'broken', '2026-10-15T10:00:00', True])
def test_rejected_dates_degrade_claimed_complete_coverage(stamp):
    report = build_activity_report(['alice'], [dict(item(), timestamp=stamp)], [], full('alice'))
    assert report['diagnostics'][0]['reason'] == 'invalid_timestamp'
    assert report['totals']['exact_total'] is None


def test_missing_author_diagnostic():
    report = build_activity_report(['alice'], [dict(item(), ownerUsername=None)], [], full('alice'))
    assert report['diagnostics'][0]['reason'] == 'missing_profile'
    assert report['totals']['exact_total'] is None


@pytest.mark.parametrize('url', ['https://evil.example/reel/ABC/', 'https://instagram.com.evil.example/reel/ABC/',
                                 'https://instagram.com@evil.example/reel/ABC/'])
def test_foreign_url_never_supplies_shortcode_profile_or_reel_type(url):
    result = normalize_activity_item(dict(item(), url=url, type='Video'), 'posts', ['alice'])
    assert result['record']['url'] is None
    assert result['record']['kind'] == 'unknown'
    assert result['record']['identifiers'] == ['id:1']
    result = normalize_activity_item(dict(item(None), url=url), 'posts', ['alice'])
    assert result['reason'] == 'missing_identity'


@pytest.mark.parametrize('extra', [{'productType': 'clips'}, {'url': 'https://www.instagram.com/reel/ABC/'}])
def test_explicit_reel_signals(extra):
    result = normalize_activity_item(dict(item(), **extra), 'posts', ['alice'])
    assert result['record']['kind'] == 'reels'


def test_selected_input_profile_takes_precedence_over_foreign_owner():
    result = normalize_activity_item(dict(item(), ownerUsername=None, inputUrl='https://instagram.com/bob/'), 'posts', ['bob'])
    assert result['record']['profile'] == 'bob'


def test_conflicting_duplicate_dates_are_diagnostic_and_not_exact():
    data = [item(), dict(item(), timestamp='2026-10-16T10:00:00Z')]
    report = build_activity_report(['alice'], data, [], full('alice'))
    assert report['totals']['observed_total'] == 1
    assert report['totals']['exact_total'] is None
    assert report['diagnostics'][0]['reason'] == 'conflicting_dates'


def test_bad_period_and_username_are_rejected():
    with pytest.raises(ValueError):
        build_activity_report(['alice'], [], [], {}, start_date='2026-10-18')
    with pytest.raises(ValueError):
        build_activity_report(['alice/evil'], [], [], {})
