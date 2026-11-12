import insights


def market(name, vac, rent, absorption):
    def val(v, unit, source='JLL'):
        return {'value': v, 'unit': unit, 'period': 'yr', 'page': 2, 'doc': {'source': source}}
    return {'market': name, 'region': 'Texas', 'quarter': 'Q3 2026', 'segments': [], 'metrics': {
        'vacancy_rate': val(vac, 'percent'), 'asking_rent': val(rent, 'usd_psf'),
        'net_absorption_qtr': val(absorption, 'sf')}}


MARKETS = [market('Dallas-Fort Worth', 7.2, 9.85, 6_412_300), market('Houston', 6.4, 8.10, 3_250_000)]


def flagged(text):
    return [f['figure'] for f in insights.unverified_figures(text, MARKETS, extra_allowed=[92])]


def test_figures_from_the_data_pass():
    text = ('Dallas-Fort Worth posted 7.2% vacancy with asking rents of $9.85/SF and 6.4 MSF of net absorption '
            '(6,412,300 SF) in Q3 2026. Houston, at 6.4%, is the tightest of the 2 markets. Score: 92.')
    assert flagged(text) == []


def test_outside_statistics_are_flagged():
    text = ('Dallas vacancy of 7.2% compares with a national average of 6.8%. Steel tariffs of 50% and '
            'construction costs near $82/SF add pressure, with transformer lead times of 18-24 months.')
    out = flagged(text)
    assert '6.8%' in out and '50%' in out and '$82' in out
    assert '7.2%' not in out


def test_market_line_cites_sources():
    line = insights.market_line(MARKETS[0])
    assert 'Vacancy rate: 7.2% [JLL, p.2]' in line and '6,412,300 SF' in line
