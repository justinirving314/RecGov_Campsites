from datetime import date

import pytest

from campwatch.availability import (
    find_stays,
    merge_months,
    months_for,
    normalize_site,
    parse_site_range,
    site_matcher,
)
from campwatch.providers import Campsite


def site(campsite_id: str, name: str, open_nights: list[date], **kw) -> Campsite:
    return Campsite(
        campsite_id=campsite_id,
        site=name,
        loop="A",
        availability={night: "Available" for night in open_nights},
        **kw,
    )


def d(day: int, month: int = 7) -> date:
    return date(2027, month, day)


def test_months_for_spans_month_boundary():
    assert months_for([(d(30), d(2, 8))]) == [d(1), d(1, 8)]


def test_months_for_excludes_departure_month():
    # Departing Aug 1 means the last night is Jul 31, so August isn't needed.
    assert months_for([(d(30), d(1, 8))]) == [d(1)]


def test_months_for_dedupes_across_ranges():
    assert months_for([(d(3), d(5)), (d(10), d(12))]) == [d(1)]


def test_whole_stay_requires_every_night_at_one_site():
    sites = [site("1", "001", [d(10), d(11)]), site("2", "002", [d(10)])]
    stays = find_stays(sites, d(10), d(12))
    assert [(s.site, s.check_in, s.check_out) for s in stays] == [("001", d(10), d(12))]


def test_whole_stay_not_split_across_sites():
    sites = [site("1", "001", [d(10)]), site("2", "002", [d(11)])]
    assert find_stays(sites, d(10), d(12)) == []


def test_flexible_reports_longest_runs():
    sites = [site("1", "001", [d(2), d(3), d(4), d(6)])]
    stays = find_stays(sites, d(1), d(8), min_nights=2)
    assert [(s.check_in, s.check_out, s.nights) for s in stays] == [(d(2), d(5), 3)]


def test_flexible_single_night_finds_each_run():
    sites = [site("1", "001", [d(2), d(6)])]
    stays = find_stays(sites, d(1), d(8), min_nights=1)
    assert [(s.check_in, s.check_out) for s in stays] == [(d(2), d(3)), (d(6), d(7))]


def test_min_nights_longer_than_window_means_whole_window():
    sites = [site("1", "001", [d(10), d(11)])]
    assert len(find_stays(sites, d(10), d(12), min_nights=5)) == 1


def test_run_ending_at_window_edge_is_clipped_to_window():
    sites = [site("1", "001", [d(9), d(10), d(11), d(12)])]
    stays = find_stays(sites, d(10), d(12))
    assert (stays[0].check_in, stays[0].check_out) == (d(10), d(12))


def test_site_filter_ignores_leading_zeros_and_case():
    sites = [site("1", "012", [d(10)]), site("2", "a5", [d(10)]), site("3", "013", [d(10)])]
    stays = find_stays(sites, d(10), d(11), sites=["12", "A5"])
    assert sorted(s.site for s in stays) == ["012", "a5"]


def test_day_use_sites_are_skipped():
    sites = [site("1", "001", [d(10)], type_of_use="Day")]
    assert find_stays(sites, d(10), d(11)) == []


def test_other_statuses_are_not_bookable():
    s = Campsite(campsite_id="1", site="001", availability={d(10): "Not Reservable"})
    assert find_stays([s], d(10), d(11)) == []


def test_merge_months_allows_stays_across_months():
    july = [site("1", "001", [d(31)])]
    august = [site("1", "001", [d(1, 8)])]
    merged = merge_months([july, august])
    stays = find_stays(merged, d(31), d(2, 8))
    assert [(s.check_in, s.check_out) for s in stays] == [(d(31), d(2, 8))]


def test_merge_months_does_not_mutate_input():
    july = [site("1", "001", [d(31)])]
    merge_months([july, [site("1", "001", [d(1, 8)])]])
    assert list(july[0].availability) == [d(31)]


def test_normalize_site():
    assert normalize_site(" 007 ") == "7"
    assert normalize_site("000") == "0"
    assert normalize_site("b12") == "B12"


def test_minimum_stay_rules_are_respected():
    s = Campsite(
        campsite_id="1",
        site="001",
        availability={d(10): "Available", d(11): "Available", d(14): "Available"},
        min_stay={d(10): 2, d(14): 2},
    )
    stays = find_stays([s], d(1), d(20), min_nights=1)
    # Jul 10-12 meets the 2-night minimum; a lone Jul 14 night doesn't.
    assert [(x.check_in, x.check_out) for x in stays] == [(d(10), d(12))]


def test_merge_months_keeps_minimum_stays():
    a = Campsite(campsite_id="1", site="1", availability={d(31): "Available"}, min_stay={d(31): 2})
    b = Campsite(campsite_id="1", site="1", availability={d(1, 8): "Available"})
    [merged] = merge_months([[a], [b]])
    assert merged.min_stay == {d(31): 2}


@pytest.mark.parametrize(
    ("entries", "name", "expected"),
    [
        (["10-12"], "010", True),
        (["10-12"], "11", True),
        (["10-12"], "12A", True),  # a lettered variant of site 12
        (["10-12"], "13", False),
        (["10-12"], "A11", False),  # different prefix
        (["A1-A10"], "a05", True),
        (["A1-10"], "A10", True),  # prefix may be left off the end
        (["A1-A10"], "B5", False),
        (["A1-A10"], "5", False),
        (["B-7"], "B-7", True),  # a site literally named "B-7", not a range
        (["B-7"], "B6", False),
        (["3", "20 - 22"], "021", True),
        (["3", "20 - 22"], "003", True),
        (["Walk-in 4"], "WALK-IN 4", True),
        (["15-12"], "13", True),  # backwards is rejected by the API but still matches here
    ],
)
def test_site_matcher(entries, name, expected):
    assert site_matcher(entries)(name) is expected


def test_no_entries_means_any_site():
    assert site_matcher([]) is None
    assert site_matcher([" "]) is None


@pytest.mark.parametrize(
    ("entry", "expected"),
    [("10-25", ("", 10, 25)), ("a1 - a10", ("A", 1, 10)), ("A1-B10", None), ("A-12", None)],
)
def test_parse_site_range(entry, expected):
    assert parse_site_range(entry) == expected


def test_find_stays_with_ranges():
    sites = [site(str(i), f"{i:03d}", [d(10)]) for i in range(1, 30)]
    stays = find_stays(sites, d(10), d(11), sites=["5", "20-22"])
    assert sorted(s.site for s in stays) == ["005", "020", "021", "022"]
