"""
Verification test: normalized float calculation must match skinsearch.org.

Contract: USP-S | Orange Anolis (MW, float=0.1233) ×8
          AWP | Exothermic (FT, float=0.2807) ×1
          USP-S | Sleeping Potion (FT, float=0.2096) ×1

Expected outputs (from skinsearch.org):
  SSG 08 | Death Strike — Field-Tested, float=0.2693
  UMP-45 | Fade — Factory New, float=0.02693
  M4A1-S | Party Animal — Field-Tested, float=0.2019

Bug: bot used simple average of absolute floats instead of
     average of per-skin normalized floats, producing wrong wear levels.
"""

import pytest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class MockSkinData:
    def __init__(self, name, min_float=0.0, max_float=1.0, wears=None, collection="TestCol", rarity="Restricted"):
        self.name = name
        self.min_float = min_float
        self.max_float = max_float
        self.wears = wears or ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
        self.collection = collection
        self.rarity = rarity


class MockDatabase:
    def __init__(self, skins):
        self._skins = {s.name: s for s in skins}

    def get_skin_by_name(self, name):
        return self._skins.get(name)

    def get_collection_skins(self, collection):
        return [s for s in self._skins.values() if s.collection == collection]

    def _normalize_rarity(self, r):
        return r


class MockPriceManager:
    def get_price(self, *args, **kwargs):
        return None

    def get_liquidity_metrics(self, *args, **kwargs):
        return {"listings_count": 0, "p10_price": None, "min_price": None}

    def get_effective_sell_price(self, *args, **kwargs):
        return 1.0


# ── Skin float ranges from skins.json ──────────────────────────────
# Input skins
ORANGE_ANOLIS_MIN, ORANGE_ANOLIS_MAX = 0.0, 0.37
EXOTHERMIC_MIN, EXOTHERMIC_MAX = 0.0, 0.7
SLEEPING_POTION_MIN, SLEEPING_POTION_MAX = 0.0, 0.7

# Output skins
DEATH_STRIKE_MIN, DEATH_STRIKE_MAX = 0.0, 0.8
FADE_MIN, FADE_MAX = 0.0, 0.08
PARTY_ANIMAL_MIN, PARTY_ANIMAL_MAX = 0.0, 0.6


def _make_input_skins():
    """Build the 10-skin input list matching the user's contract."""
    skins = []
    for _ in range(8):
        skins.append({
            'name': 'USP-S | Orange Anolis',
            'float': 0.1233,
            'wear': 'Minimal Wear',
            'collection': 'Clutch',
            'rarity': 'Restricted',
        })
    skins.append({
        'name': 'AWP | Exothermic',
        'float': 0.2807,
        'wear': 'Field-Tested',
        'collection': 'Clutch',
        'rarity': 'Restricted',
    })
    skins.append({
        'name': 'USP-S | Sleeping Potion',
        'float': 0.2096,
        'wear': 'Field-Tested',
        'collection': 'Clutch',
        'rarity': 'Restricted',
    })
    return skins


class TestNormalizedFloatCalculation:
    """
    Verify that _calculate_average_normalized_float produces the correct
    avg normalized value, and that calculate_contract_outcomes_details
    uses it to compute correct output floats and wears.
    """

    def test_normalized_float_formula_pure(self):
        """
        Pure math verification: avg_normalized = 0.3366

        Input normalizations:
          Orange Anolis:  (0.1233 - 0.0) / (0.37 - 0.0) = 0.33324..  ×8
          Exothermic:     (0.2807 - 0.0) / (0.70 - 0.0) = 0.40100..
          Sleeping Potion:(0.2096 - 0.0) / (0.70 - 0.0) = 0.29943..

        avg_norm = (8×0.33324 + 0.40100 + 0.29943) / 10 ≈ 0.3366
        """
        norm_anolis = (0.1233 - ORANGE_ANOLIS_MIN) / (ORANGE_ANOLIS_MAX - ORANGE_ANOLIS_MIN)
        norm_exothermic = (0.2807 - EXOTHERMIC_MIN) / (EXOTHERMIC_MAX - EXOTHERMIC_MIN)
        norm_sleeping = (0.2096 - SLEEPING_POTION_MIN) / (SLEEPING_POTION_MAX - SLEEPING_POTION_MIN)

        avg_norm = (8 * norm_anolis + norm_exothermic + norm_sleeping) / 10

        assert abs(norm_anolis - 0.33324) < 1e-3
        assert abs(norm_exothermic - 0.4010) < 1e-3
        assert abs(norm_sleeping - 0.2994) < 1e-3
        assert abs(avg_norm - 0.3366) < 1e-2

    def test_output_floats_match_skinsearch(self):
        """
        out_float = avg_norm × (max - min) + min  must match skinsearch.org:

          SSG 08 Death Strike:   0.3366 × 0.80 + 0.0 = 0.2693
          UMP-45 Fade:          0.3366 × 0.08 + 0.0 = 0.02693
          M4A1-S Party Animal:  0.3366 × 0.60 + 0.0 = 0.2019
        """
        avg_norm = 0.3366  # approximate

        ds_float = avg_norm * (DEATH_STRIKE_MAX - DEATH_STRIKE_MIN) + DEATH_STRIKE_MIN
        fade_float = avg_norm * (FADE_MAX - FADE_MIN) + FADE_MIN
        pa_float = avg_norm * (PARTY_ANIMAL_MAX - PARTY_ANIMAL_MIN) + PARTY_ANIMAL_MIN

        assert abs(ds_float - 0.2693) < 5e-3, f"SSG 08 Death Strike: expected ~0.2693, got {ds_float:.4f}"
        assert abs(fade_float - 0.02693) < 5e-3, f"UMP-45 Fade: expected ~0.02693, got {fade_float:.5f}"
        assert abs(pa_float - 0.2019) < 5e-3, f"M4A1-S Party Animal: expected ~0.2019, got {pa_float:.4f}"

    def test_output_wears_correct(self):
        """Wear determination from skinsearch-verified floats."""
        assert 0.15 < 0.2693 <= 0.38  # FT
        assert 0.02693 <= 0.07        # FN
        assert 0.15 < 0.2019 <= 0.38  # FT

    def test_old_formula_gives_wrong_wears(self):
        """
        Demonstrates the bug: old formula (avg of absolute floats)
        gives 0.14767, producing wrong output floats and wears.
        """
        avg_abs = (8 * 0.1233 + 0.2807 + 0.2096) / 10  # = 0.14767

        ds_float_old = avg_abs * (DEATH_STRIKE_MAX - DEATH_STRIKE_MIN) + DEATH_STRIKE_MIN
        pa_float_old = avg_abs * (PARTY_ANIMAL_MAX - PARTY_ANIMAL_MIN) + PARTY_ANIMAL_MIN

        # Old results: 0.1181 → MW (WRONG), 0.0886 → MW (WRONG)
        assert ds_float_old <= 0.15, "Old: SSG Death Strike would be MW (BUG)"
        assert pa_float_old <= 0.15, "Old: Party Animal would be MW (BUG)"

        # Correct results: 0.2693 → FT, 0.2019 → FT
        correct_ds = 0.3366 * 0.8 + 0.0
        correct_pa = 0.3366 * 0.6 + 0.0
        assert 0.15 < correct_ds <= 0.38, "Correct: SSG Death Strike = FT"
        assert 0.15 < correct_pa <= 0.38, "Correct: Party Animal = FT"


class TestContractOutcomesDetailsNormalized:
    """
    Integration test: calculate_contract_outcomes_details must use
    _calculate_average_normalized_float and produce correct output
    floats and wear levels matching skinsearch.org.
    """

    def test_full_contract_outcomes(self):
        """End-to-end verification of the Orange Anolis contract."""
        from calculator import ContractCalculator as Calculator

        # Set up mock skins
        input_skins_data = [
            MockSkinData('USP-S | Orange Anolis', min_float=ORANGE_ANOLIS_MIN, max_float=ORANGE_ANOLIS_MAX,
                         wears=['Factory New', 'Minimal Wear', 'Field-Tested'], collection='Clutch', rarity='Restricted'),
            MockSkinData('AWP | Exothermic', min_float=EXOTHERMIC_MIN, max_float=EXOTHERMIC_MAX,
                         collection='Clutch', rarity='Restricted'),
            MockSkinData('USP-S | Sleeping Potion', min_float=SLEEPING_POTION_MIN, max_float=SLEEPING_POTION_MAX,
                         collection='Clutch', rarity='Restricted'),
            # Output skins
            MockSkinData('SSG 08 | Death Strike', min_float=DEATH_STRIKE_MIN, max_float=DEATH_STRIKE_MAX,
                         collection='Clutch', rarity='Classified'),
            MockSkinData('UMP-45 | Fade', min_float=FADE_MIN, max_float=FADE_MAX,
                         wears=['Factory New', 'Minimal Wear'], collection='Clutch', rarity='Classified'),
            MockSkinData('M4A1-S | Party Animal', min_float=PARTY_ANIMAL_MIN, max_float=PARTY_ANIMAL_MAX,
                         collection='Clutch', rarity='Classified'),
        ]

        mock_db = MockDatabase(input_skins_data)
        mock_price = MockPriceManager()
        calc = Calculator(mock_db, mock_price)
        calc.database = mock_db
        calc.price_manager = mock_price

        input_skins = _make_input_skins()

        # Override _get_possible_outputs to return our output skins
        calc._get_possible_outputs = lambda collection, input_rarity, average_float, is_stattrak: [
            {'name': 'SSG 08 | Death Strike'},
            {'name': 'UMP-45 | Fade'},
            {'name': 'M4A1-S | Party Animal'},
        ]

        outcomes = calc.calculate_contract_outcomes_details(input_skins, is_stattrak=False)

        # Build lookup
        outcome_map = {o['name']: o for o in outcomes}

        # Verify SSG 08 | Death Strike
        ds = outcome_map.get('SSG 08 | Death Strike')
        assert ds is not None, "SSG 08 | Death Strike should be in outcomes"
        assert abs(ds['out_float'] - 0.2693) < 5e-3, \
            f"SSG 08 Death Strike float: expected ~0.2693, got {ds['out_float']:.4f}"
        assert ds['wear'] == 'Field-Tested', \
            f"SSG 08 Death Strike wear: expected Field-Tested, got {ds['wear']}"

        # Verify UMP-45 | Fade
        fade = outcome_map.get('UMP-45 | Fade')
        assert fade is not None, "UMP-45 Fade should be in outcomes"
        assert abs(fade['out_float'] - 0.02693) < 5e-3, \
            f"UMP-45 Fade float: expected ~0.02693, got {fade['out_float']:.5f}"
        assert fade['wear'] == 'Factory New', \
            f"UMP-45 Fade wear: expected Factory New, got {fade['wear']}"

        # Verify M4A1-S | Party Animal
        pa = outcome_map.get('M4A1-S | Party Animal')
        assert pa is not None, "M4A1-S Party Animal should be in outcomes"
        assert abs(pa['out_float'] - 0.2019) < 5e-3, \
            f"M4A1-S Party Animal float: expected ~0.2019, got {pa['out_float']:.4f}"
        assert pa['wear'] == 'Field-Tested', \
            f"M4A1-S Party Animal wear: expected Field-Tested, got {pa['wear']}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
