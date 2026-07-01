"""
Test: verify bot output wear for a specific contract with StatTrak Sawed-Off Wasteland Princess inputs.
Inputs: 7x BS, 1x WW, 2x MW (all StatTrak).
Expected bot outputs: M4A4 Buzz Kill = BS, SSG 08 Dragonfire = WW.
"""

import pytest
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from unittest.mock import MagicMock, patch
from calculator import ContractCalculator


class MockSkinData:
    def __init__(self, name, min_float=0.0, max_float=1.0, collection="TestCollection", rarity="Classified"):
        self.name = name
        self.min_float = min_float
        self.max_float = max_float
        self.wears = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
        self.collection = collection
        self.rarity = rarity


def test_stattrak_wasteland_princess_contract():
    input_skins = [
        {"name": "StatTrak\u2122 Sawed-Off | Wasteland Princess", "float": None, "wear": "Battle-Scarred", "collection": "TestCollection", "rarity": "Restricted"},
    ] * 7 + [
        {"name": "StatTrak\u2122 Sawed-Off | Wasteland Princess", "float": None, "wear": "Well-Worn", "collection": "TestCollection", "rarity": "Restricted"},
    ] * 1 + [
        {"name": "StatTrak\u2122 Sawed-Off | Wasteland Princess", "float": None, "wear": "Minimal Wear", "collection": "TestCollection", "rarity": "Restricted"},
    ] * 2

    skins_db = {
        "StatTrak\u2122 Sawed-Off | Wasteland Princess": MockSkinData(
            name="StatTrak\u2122 Sawed-Off | Wasteland Princess",
            min_float=0.0, max_float=0.7, collection="TestCollection", rarity="Restricted",
        ),
        "StatTrak\u2122 M4A4 | Buzz Kill": MockSkinData(
            name="StatTrak\u2122 M4A4 | Buzz Kill",
            min_float=0.0, max_float=0.7, collection="TestCollection", rarity="Classified",
        ),
        "StatTrak\u2122 SSG 08 | Dragonfire": MockSkinData(
            name="StatTrak\u2122 SSG 08 | Dragonfire",
            min_float=0.0, max_float=0.5, collection="TestCollection", rarity="Classified",
        ),
    }

    mock_db = MagicMock()
    mock_db.get_skin_by_name = lambda n: skins_db.get(n)
    mock_db.get_collection_skins = lambda c: list(skins_db.values())
    mock_db._normalize_rarity = lambda r: r
    mock_db.list_collections = lambda: ["TestCollection"]

    mock_pm = MagicMock()
    mock_pm.get_effective_sell_price = MagicMock(return_value=None)
    mock_pm.get_price = MagicMock(return_value=None)

    calc = ContractCalculator(mock_db, mock_pm)

    outcomes = calc.calculate_contract_outcomes_details(input_skins, is_stattrak=True)

    wears_by_name = {o['name']: o['wear'] for o in outcomes}

    assert wears_by_name.get("StatTrak\u2122 M4A4 | Buzz Kill") == "Battle-Scarred", \
        f"M4A4 Buzz Kill: expected BS, got {wears_by_name.get('StatTrak\u2122 M4A4 | Buzz Kill')}"
    assert wears_by_name.get("StatTrak\u2122 SSG 08 | Dragonfire") == "Well-Worn", \
        f"SSG 08 Dragonfire: expected WW, got {wears_by_name.get('StatTrak\u2122 SSG 08 | Dragonfire')}"
