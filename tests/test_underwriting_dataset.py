"""Checks on underwriting_markets.json — catches missing fields and typos
(e.g. a rent of 140 instead of 14.0) before they reach someone's pro forma.

Run with:  python -m unittest discover tests
"""
import json
import os
import unittest

PATH = os.path.join(os.path.dirname(__file__), '..', 'underwriting_markets.json')

FIELDS = {
    'rent': (2, 40), 'cost': (30, 250), 'free_rent': (0, 12), 'ti_new': (0, 40),
    'ti_renewal': (0, 25), 'lc': (0, 10), 'vacancy': (0, 30), 'rent_growth': (-10, 15),
    'lease_term': (12, 180), 'renewal_prob': (30, 100), 'downtime': (0, 36), 'cap': (3, 10),
    'opex': (0, 5), 'taxes': (0, 5), 'cap_reserve': (0, 1),
}


class UnderwritingDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PATH, encoding='utf-8') as fh:
            cls.data = json.load(fh)
        cls.markets = cls.data['markets']

    def test_top_level(self):
        self.assertRegex(self.data['quarter'], r'^Q[1-4] \d{4}$')
        self.assertTrue(self.data['sources'])
        self.assertGreater(len(self.markets), 0)

    def test_market_fields(self):
        for name, m in self.markets.items():
            with self.subTest(market=name):
                self.assertIn(m['tier'], ('Primary', 'Secondary', 'Avoid'))
                self.assertTrue(0 <= m['score'] <= 100)
                self.assertTrue(m['report_url'].startswith('https://'))
                for k in ('vacancy', 'absorption', 'rent_growth', 'cap_rate', 'pipeline'):
                    self.assertIsInstance(m[k], (int, float))

    def test_size_segments(self):
        sizes = sorted(next(iter(self.markets.values()))['by_size'])
        self.assertEqual(len(sizes), 6)
        for name, m in self.markets.items():
            self.assertEqual(sorted(m['by_size']), sizes, name)
            for size, d in m['by_size'].items():
                for field, (lo, hi) in FIELDS.items():
                    v = d.get(field)
                    with self.subTest(market=name, size=size, field=field):
                        self.assertIsInstance(v, (int, float))
                        self.assertTrue(lo <= v <= hi, f'{v} outside {lo}-{hi}')


if __name__ == '__main__':
    unittest.main()
