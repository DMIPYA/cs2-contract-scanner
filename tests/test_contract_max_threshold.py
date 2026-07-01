"""
Тесты для проверки расчёта максимально допустимого avg_norm для контрактов.

Покрывает план из 1780686754443-crisp-island.md:
- calculate_max_avg_float_for_outcomes() - единая формула max-границы
- calculate_expected_wear_from_outcomes() - ожидаемый wear из outcomes
- Граничные случаи: разные wears_avail, min_float/max_float, BS-only скины
"""

import pytest
from unittest.mock import MagicMock, patch
from typing import List, Dict, Optional


class MockSkinData:
    """Mock для SkinData из database.py"""
    def __init__(
        self,
        name: str,
        min_float: float = 0.0,
        max_float: float = 1.0,
        wears: Optional[List[str]] = None,
        collection: str = "Test Collection",
        rarity: str = "Restricted",
    ):
        self.name = name
        self.min_float = min_float
        self.max_float = max_float
        self.wears = wears or ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
        self.collection = collection
        self.rarity = rarity


class TestCalculateMaxAvgFloatForOutcomes:
    """Тесты для calculate_max_avg_float_for_outcomes()"""
    
    @pytest.fixture
    def calculator(self):
        """Создаёт мок калькулятора с минимальной конфигурацией"""
        with patch('calculator.CS2Database') as MockDB, \
             patch('calculator.PriceManager') as MockPM:
            from calculator import ContractCalculator
            
            db = MagicMock()
            pm = MagicMock()
            calc = ContractCalculator(db, pm)
            return calc
    
    def _make_outcome(
        self,
        name: str = "Test Skin",
        min_float: float = 0.0,
        max_float: float = 1.0,
        wear: str = "Factory New",
        probability: float = 0.1,
        price: float = 10.0,
    ) -> Dict:
        """Создаёт мок outcome для тестов"""
        return {
            "name": name,
            "min_float": min_float,
            "max_float": max_float,
            "wear": wear,
            "probability": probability,
            "price": price,
            "collection": "Test Collection",
            "out_float": 0.0,
            "sell_source": "MARKETCSGO",
            "sell_fee": 0.07,
        }
    
    def test_single_outcome_fn_target(self, calculator):
        """Один outcome с FN target должен возвращать ~0.07 с safety margin"""
        outcomes = [self._make_outcome("Skin A", min_float=0.0, max_float=1.0)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData("Skin A", min_float=0.0, max_float=1.0)
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Factory New")
        
        assert result is not None
        assert 0.069 < result < 0.07
    
    def test_single_outcome_mw_target(self, calculator):
        """Один outcome с MW target должен возвращать ~0.15 с safety margin"""
        outcomes = [self._make_outcome("Skin A", min_float=0.0, max_float=1.0)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData("Skin A", min_float=0.0, max_float=1.0)
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Minimal Wear")
        
        assert result is not None
        assert 0.149 < result < 0.15
    
    def test_single_outcome_limited_range(self, calculator):
        """Скин с ограниченным float-диапазоном должен учитывать min/max"""
        outcomes = [self._make_outcome("Skin A", min_float=0.06, max_float=0.08)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData("Skin A", min_float=0.06, max_float=0.08)
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Factory New")
        
        assert result is not None
        max_expected = (0.07 - 0.06) / (0.08 - 0.06)
        assert result < max_expected
    
    def test_multiple_outcomes_bottleneck(self, calculator):
        """Несколько outcomes: самый строгий (bottleneck) определяет max"""
        outcomes = [
            self._make_outcome("Skin A", min_float=0.0, max_float=1.0),
            self._make_outcome("Skin B", min_float=0.0, max_float=0.5),
        ]
        
        def get_skin(name):
            if name == "Skin A":
                return MockSkinData("Skin A", min_float=0.0, max_float=1.0)
            return MockSkinData("Skin B", min_float=0.0, max_float=0.5)
        
        calculator.database.get_skin_by_name = MagicMock(side_effect=get_skin)
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Factory New")
        
        assert result is not None
        max_for_b = 0.07 / 0.5
        assert result < max_for_b
    
    def test_outcome_without_fn_wear(self, calculator):
        """Скин без FN в wears_avail при FN target должен возвращать None (гарантия невозможна)"""
        outcomes = [self._make_outcome("Skin A", min_float=0.0, max_float=1.0)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData(
                "Skin A",
                min_float=0.0,
                max_float=1.0,
                wears=["Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
            )
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Factory New")
        
        assert result is None
    
    def test_bs_only_skin(self, calculator):
        """Скин только с BS должен возвращать max_f"""
        outcomes = [self._make_outcome("Skin A", min_float=0.45, max_float=1.0)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData(
                "Skin A",
                min_float=0.45,
                max_float=1.0,
                wears=["Battle-Scarred"]
            )
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Battle-Scarred")
        
        assert result is not None
        assert result >= 0.998
    
    def test_no_valid_outcomes_returns_none(self, calculator):
        """Нет валидных outcomes должен возвращать None"""
        outcomes = []
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Factory New")
        
        assert result is None
    
    def test_skin_without_wears_uses_full_range(self, calculator):
        """Скин без wears_avail должен использовать полный набор wear"""
        outcomes = [self._make_outcome("Skin A", min_float=0.0, max_float=1.0)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData(
                "Skin A",
                min_float=0.0,
                max_float=1.0,
                wears=None
            )
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Factory New")
        
        assert result is not None
        assert 0.069 < result < 0.07
    
    def test_boundary_0_07(self, calculator):
        """Граничное условие: float точно на границе 0.07"""
        outcomes = [self._make_outcome("Skin A", min_float=0.0, max_float=0.07)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData("Skin A", min_float=0.0, max_float=0.07)
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Factory New")
        
        assert result is not None
        assert result >= 0.998
    
    def test_boundary_0_15(self, calculator):
        """Граничное условие: float точно на границе 0.15"""
        outcomes = [self._make_outcome("Skin A", min_float=0.07, max_float=0.15)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData("Skin A", min_float=0.07, max_float=0.15)
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Minimal Wear")
        
        assert result is not None
        assert result >= 0.998
    
    def test_boundary_0_38(self, calculator):
        """Граничное условие: float точно на границе 0.38"""
        outcomes = [self._make_outcome("Skin A", min_float=0.15, max_float=0.38)]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData("Skin A", min_float=0.15, max_float=0.38)
        )
        
        result = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Field-Tested")
        
        assert result is not None
        assert result >= 0.998


class TestCalculateExpectedWearFromOutcomes:
    """Тесты для calculate_expected_wear_from_outcomes()"""
    
    @pytest.fixture
    def calculator(self):
        """Создаёт мок калькулятора"""
        with patch('calculator.CS2Database') as MockDB, \
             patch('calculator.PriceManager') as MockPM:
            from calculator import ContractCalculator
            
            db = MagicMock()
            pm = MagicMock()
            calc = ContractCalculator(db, pm)
            return calc
    
    def _make_outcome(self, wear: str) -> Dict:
        """Создаёт outcome с заданным wear"""
        return {"name": "Test", "wear": wear, "probability": 0.1, "price": 10.0}
    
    def test_all_fn_returns_fn(self, calculator):
        """Все outcomes FN должны возвращать FN"""
        outcomes = [
            self._make_outcome("Factory New"),
            self._make_outcome("Factory New"),
        ]
        
        result = calculator.calculate_expected_wear_from_outcomes(outcomes)
        
        assert result == "Factory New"
    
    def test_mixed_wears_returns_worst(self, calculator):
        """Смешанные wears должны возвращать худший"""
        outcomes = [
            self._make_outcome("Factory New"),
            self._make_outcome("Minimal Wear"),
            self._make_outcome("Field-Tested"),
        ]
        
        result = calculator.calculate_expected_wear_from_outcomes(outcomes)
        
        assert result == "Field-Tested"
    
    def test_bs_is_worst(self, calculator):
        """BS должен быть худшим wear"""
        outcomes = [
            self._make_outcome("Factory New"),
            self._make_outcome("Battle-Scarred"),
        ]
        
        result = calculator.calculate_expected_wear_from_outcomes(outcomes)
        
        assert result == "Battle-Scarred"
    
    def test_empty_outcomes_returns_none(self, calculator):
        """Пустой список outcomes должен возвращать None"""
        result = calculator.calculate_expected_wear_from_outcomes([])
        
        assert result is None
    
    def test_unknown_wear_defaults_to_bs(self, calculator):
        """Неизвестный wear должен деградировать до BS"""
        outcomes = [self._make_outcome("Unknown Wear")]
        
        result = calculator.calculate_expected_wear_from_outcomes(outcomes)
        
        assert result == "Battle-Scarred"


class TestMaxThresholdConsistency:
    """Интеграционные тесты для проверки консистентности max между слоями"""
    
    @pytest.fixture
    def calculator(self):
        """Создаёт мок калькулятора"""
        with patch('calculator.CS2Database') as MockDB, \
             patch('calculator.PriceManager') as MockPM:
            from calculator import ContractCalculator
            
            db = MagicMock()
            pm = MagicMock()
            calc = ContractCalculator(db, pm)
            return calc
    
    def test_max_does_not_degrade_any_outcome(self, calculator):
        """При использовании max ни один outcome не должен ухудшить wear"""
        outcomes = [
            {
                "name": "Skin A",
                "min_float": 0.0,
                "max_float": 1.0,
                "wear": "Factory New",
                "probability": 0.5,
                "price": 10.0,
            },
            {
                "name": "Skin B",
                "min_float": 0.0,
                "max_float": 0.5,
                "wear": "Factory New",
                "probability": 0.5,
                "price": 15.0,
            },
        ]
        
        calculator.database.get_skin_by_name = MagicMock(
            return_value=MockSkinData("Test", min_float=0.0, max_float=1.0)
        )
        
        max_threshold = calculator.calculate_max_avg_float_for_outcomes(outcomes, "Factory New")
        
        assert max_threshold is not None
        
        for o in outcomes:
            min_f = o["min_float"]
            max_f = o["max_float"]
            out_float_at_max = max(min_f, min(max_f, max_threshold))
            
            if out_float_at_max <= 0.07:
                expected_wear = "Factory New"
            elif out_float_at_max <= 0.15:
                expected_wear = "Minimal Wear"
            elif out_float_at_max <= 0.38:
                expected_wear = "Field-Tested"
            elif out_float_at_max <= 0.45:
                expected_wear = "Well-Worn"
            else:
                expected_wear = "Battle-Scarred"
            
            assert expected_wear == "Factory New", f"Outcome {o['name']} would degrade to {expected_wear}"
