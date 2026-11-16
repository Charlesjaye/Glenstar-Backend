import math

import pytest

import sizes

INF = math.inf


@pytest.mark.parametrize('label,expected', [
    ('100,000-249,999 SF', (100_000, 250_000)),
    ('100,000 - 250,000 sq. ft.', (100_000, 250_000)),
    ('100K-250K SF', (100_000, 250_000)),
    ('100-250K', (100_000, 250_000)),
    ('100k – 249.9k', (100_000, 249_900)),
    ('Under 100,000 SF', (0, 100_000)),
    ('< 50K SF', (0, 50_000)),
    ('Less than 20,000 square feet', (0, 20_000)),
    ('500,000 SF and above', (500_000, INF)),
    ('750K+', (750_000, INF)),
    ('1M+ SF', (1_000_000, INF)),
    ('Over 1 million SF', (1_000_000, INF)),
    ('1,000,000+', (1_000_000, INF)),
    ('500,000-999,999 SF', (500_000, 1_000_000)),
    ('20-50', (20_000, 50_000)),
    ('0-49,999 SF', (0, 50_000)),
    ('Big Box (500K+)', (500_000, INF)),
    ('Small-bay: under 50,000 SF', (0, 50_000)),
])
def test_size_ranges_are_read_from_broker_labels(label, expected):
    assert sizes.parse_size_range(label) == expected


@pytest.mark.parametrize('label', ['Warehouse/Distribution', 'Flex', 'Manufacturing', 'Class A', '', 'Total', 'Q3 2026'])
def test_non_size_labels_are_not_forced_into_a_band(label):
    assert sizes.parse_size_range(label) is None


def test_classify():
    assert sizes.classify('100,000-249,999 SF') == {'kind': 'size', 'size_min': 100_000, 'size_max': 250_000}
    assert sizes.classify('750K+')['size_max'] is None
    assert sizes.classify('Warehouse/Distribution')['kind'] == 'type'
    assert sizes.classify('North submarket')['kind'] == 'other'


def test_mapping_to_standard_bands():
    assert sizes.bands_for(100_000, 250_000) == (['b3'], 'exact')
    assert sizes.bands_for(100_000, 200_000) == (['b3'], 'within')       # narrower slice of one band
    assert sizes.bands_for(0, 100_000) == (['b1', 'b2'], 'spans')        # broker band wider than ours
    assert sizes.bands_for(500_000, None) == (['b5', 'b6'], 'spans')
    assert sizes.bands_for(750_000, None) == (['b6'], 'exact')
    assert sizes.bands_for(200_000, 400_000) == (['b3', 'b4'], 'spans')
    assert sizes.bands_for(None, None) == ([], None)


def test_format_range():
    assert sizes.format_range(100_000, 250_000) == '100K–250K SF'
    assert sizes.format_range(0, 50_000) == 'Under 50K SF'
    assert sizes.format_range(1_000_000, None) == '1M+ SF'
