"""
Integration test for calculate_contract_outcomes_details ensuring different wears per output skin.
"""

import pytest
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from calculator import ContractCalculator as Calculator
from telegram_bot import _wear_to_max_float, _calc_group_max_float_for_contract

class MockSkinData:
    def __init__(self, name, min_float=0.0, max_float=1.0, collection="TestCollection", rarity="Restricted"):
        self.name = name
        self.min_float = min_float
        self.max_float = max_float
        self.wears = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
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
    def get_price(self, name, wear, is_stattrak):
        return None
    def get_liquidity_metrics(self, *args, **kwargs):
        return {"listings_count": 0, "p10_price": None, "min_price": None}
    def get_effective_sell_price(self, *args, **kwargs):
        return 1.0


class MockSvc:
    def __init__(self, db):
        self.database = db


class WeightedMockCalculator(Calculator):
    def _get_possible_outputs(self, collection, input_rarity, average_float, is_stattrak):
        next_rarity = self._get_next_rarity(self._normalize_rarity(input_rarity))
        if collection == 'CollectionA' and next_rarity == 'Classified':
            return [{'name': 'OutA'}]
        if collection == 'CollectionB' and next_rarity == 'Classified':
            return [{'name': 'OutB'}]
        return []

def test_outcomes_have_distinct_wears():
    skin_a = MockSkinData(name="SkinA", min_float=0.0, max_float=1.0)
    skin_b = MockSkinData(name="SkinB", min_float=0.06, max_float=0.80, rarity="Classified")
    skin_c = MockSkinData(name="SkinC", min_float=0.38, max_float=1.0, rarity="Classified")
    mock_db = MockDatabase([skin_a, skin_b, skin_c])
    mock_price = MockPriceManager()
    calc = Calculator(mock_db, mock_price)
    calc.database = mock_db
    calc.price_manager = mock_price
    input_skins = [{"name": f"Input{i}", "float": 0.20, "collection": "TestCollection", "rarity": "Restricted"} for i in range(10)]

    calc._get_possible_outputs = lambda collection, input_rarity, average_float, is_stattrak: [
        {"name": "SkinA"},
        {"name": "SkinB"},
        {"name": "SkinC"},
    ]

    outcomes = calc.calculate_contract_outcomes_details(input_skins, False)
    wears = {o.get('wear') for o in outcomes}
    assert wears
    assert 'Field-Tested' in wears


def test_wear_to_max_float_uses_exact_ceiling():
    assert _wear_to_max_float('Factory New') == pytest.approx(0.0699)
    assert _wear_to_max_float('Minimal Wear') == pytest.approx(0.1499)
    assert _wear_to_max_float('Field-Tested') == pytest.approx(0.3799)
    assert _wear_to_max_float('Well-Worn') == pytest.approx(0.4499)
    assert _wear_to_max_float('Battle-Scarred') == pytest.approx(1.0)


def test_group_max_float_uses_contract_threshold_and_other_inputs():
    skin_a = MockSkinData(name="SkinA", min_float=0.0, max_float=1.0)
    skin_b = MockSkinData(name="SkinB", min_float=0.0, max_float=1.0)
    mock_db = MockDatabase([skin_a, skin_b])
    svc = MockSvc(mock_db)

    contract = {
        "input_skins": [
            {"name": "SkinA", "float": 0.20, "wear": "Field-Tested", "collection": "TestCollection"},
            {"name": "SkinA", "float": 0.20, "wear": "Field-Tested", "collection": "TestCollection"},
            {"name": "SkinB", "float": 0.10, "wear": "Minimal Wear", "collection": "TestCollection"},
        ],
        "is_stattrak": False,
    }
    group = {"name": "SkinA", "wear": "Field-Tested", "collection": "TestCollection"}

    thr = 0.38
    result = _calc_group_max_float_for_contract(svc=svc, contract=contract, group=group, avg_norm_thr=thr)

    assert result is not None
    # Two SkinA items share the same budget, so the per-item limit is higher than the raw wear ceiling.
    assert result == pytest.approx(0.52)


def test_weighted_outcomes_use_collection_output_range():
    input_a = MockSkinData(name='InputA', min_float=0.0, max_float=1.0, collection='CollectionA', rarity='Restricted')
    input_b = MockSkinData(name='InputB', min_float=0.0, max_float=1.0, collection='CollectionB', rarity='Restricted')
    out_a = MockSkinData(name='OutA', min_float=0.0, max_float=0.15, collection='CollectionA', rarity='Classified')
    out_b = MockSkinData(name='OutB', min_float=0.0, max_float=1.0, collection='CollectionB', rarity='Classified')
    mock_db = MockDatabase([input_a, input_b, out_a, out_b])
    mock_price = MockPriceManager()
    calc = WeightedMockCalculator(mock_db, mock_price)
    calc.database = mock_db
    calc.price_manager = mock_price

    contract = [
        {'name': 'InputA', 'float': 0.05, 'collection': 'CollectionA', 'rarity': 'Restricted'},
        {'name': 'InputA', 'float': 0.05, 'collection': 'CollectionA', 'rarity': 'Restricted'},
        {'name': 'InputA', 'float': 0.05, 'collection': 'CollectionA', 'rarity': 'Restricted'},
        {'name': 'InputA', 'float': 0.05, 'collection': 'CollectionA', 'rarity': 'Restricted'},
        {'name': 'InputA', 'float': 0.05, 'collection': 'CollectionA', 'rarity': 'Restricted'},
        {'name': 'InputA', 'float': 0.05, 'collection': 'CollectionA', 'rarity': 'Restricted'},
        {'name': 'InputA', 'float': 0.05, 'collection': 'CollectionA', 'rarity': 'Restricted'},
        {'name': 'InputA', 'float': 0.05, 'collection': 'CollectionA', 'rarity': 'Restricted'},
        {'name': 'InputB', 'float': 0.35, 'collection': 'CollectionB', 'rarity': 'Restricted'},
        {'name': 'InputB', 'float': 0.35, 'collection': 'CollectionB', 'rarity': 'Restricted'},
    ]

    outs = calc.calculate_contract_outcomes_details(contract, is_stattrak=False)
    wear_map = {o['name']: o['wear'] for o in outs}

    assert wear_map['OutA'] == 'Minimal Wear'
    assert wear_map['OutB'] == 'Field-Tested'
