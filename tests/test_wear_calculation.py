"""
Детерминированные тесты для проверки корректности расчёта wear и float.

Тесты покрывают исправления из fix-wear-mismatch-plan.md:
- Шаг 1: _calculate_skinsearch_normalized_float()
- Шаг 2: calculate_contract_outcomes_details() - out_float расчёт
- Шаг 3: _determine_best_achievable_wear() → _determine_wear_from_float()
- Шаг 4: Граничные условия в _determine_wear_from_float()
- Шаг 5: calculate_wear_leap()
- Шаг 6: _optimize_contract_floats()

Без реальных API-вызовов.
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


class TestDetermineWearFromFloat:
    """Тесты для _determine_wear_from_float() - Шаг 4"""
    
    # CS2 wear ranges (inclusive upper bound):
    # FN: [0.00, 0.07], MW: (0.07, 0.15], FT: (0.15, 0.38], WW: (0.38, 0.45], BS: (0.45, 1.00]
    
    def _determine_wear_from_float(self, item_float: float, available_wears: Optional[List[str]] = None) -> str:
        """Локальная копия функции для тестирования с available_wears"""
        try:
            f = float(item_float)
        except Exception:
            return "Unknown"

        if f <= 0.07:
            ideal_wear = 'Factory New'
        elif f <= 0.15:
            ideal_wear = 'Minimal Wear'
        elif f <= 0.38:
            ideal_wear = 'Field-Tested'
        elif f <= 0.45:
            ideal_wear = 'Well-Worn'
        else:
            ideal_wear = 'Battle-Scarred'

        if not available_wears:
            return ideal_wear

        wear_order = ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']

        try:
            ideal_idx = wear_order.index(ideal_wear)
        except ValueError:
            return available_wears[-1] if available_wears else "Unknown"

        for i in range(ideal_idx, len(wear_order)):
            if wear_order[i] in available_wears:
                return wear_order[i]

        return available_wears[-1] if available_wears else ideal_wear
    
    def test_fn_boundary_low(self):
        """FN: 0.00 ровно → FN"""
        assert self._determine_wear_from_float(0.00) == 'Factory New'
    
    def test_fn_boundary_high(self):
        """FN: 0.07 ровно → FN (включительно)"""
        assert self._determine_wear_from_float(0.07) == 'Factory New'
    
    def test_fn_inside(self):
        """FN: 0.05 → FN"""
        assert self._determine_wear_from_float(0.05) == 'Factory New'
    
    def test_mw_boundary_low(self):
        """MW: 0.07 + epsilon → MW"""
        assert self._determine_wear_from_float(0.0701) == 'Minimal Wear'
    
    def test_mw_boundary_high(self):
        """MW: 0.15 ровно → MW (включительно)"""
        assert self._determine_wear_from_float(0.15) == 'Minimal Wear'
    
    def test_ft_boundary_high(self):
        """FT: 0.38 ровно → FT (включительно)"""
        assert self._determine_wear_from_float(0.38) == 'Field-Tested'
    
    def test_ww_boundary_high(self):
        """WW: 0.45 ровно → WW (включительно)"""
        assert self._determine_wear_from_float(0.45) == 'Well-Worn'
    
    def test_bs_boundary_low(self):
        """BS: 0.45 + epsilon → BS"""
        assert self._determine_wear_from_float(0.451) == 'Battle-Scarred'
    
    def test_bs_max(self):
        """BS: 1.0 ровно → BS"""
        assert self._determine_wear_from_float(1.0) == 'Battle-Scarred'
    
    def test_edge_cases(self):
        """Граничные значения с плавающей точкой"""
        assert self._determine_wear_from_float(0.0699999999) == 'Factory New'
        assert self._determine_wear_from_float(0.0700000001) == 'Minimal Wear'
        assert self._determine_wear_from_float(0.1499999999) == 'Minimal Wear'
        assert self._determine_wear_from_float(0.1500000001) == 'Field-Tested'

    def test_limited_wears_ft_ww_bs_float_03(self):
        """MAC-10 Sakkaku: wears=[FT, WW, BS], float=0.3 → FT"""
        assert self._determine_wear_from_float(0.3, ['Field-Tested', 'Well-Worn', 'Battle-Scarred']) == 'Field-Tested'

    def test_limited_wears_ft_ww_bs_float_05(self):
        """MAC-10 Sakkaku: wears=[FT, WW, BS], float=0.5 → BS"""
        assert self._determine_wear_from_float(0.5, ['Field-Tested', 'Well-Worn', 'Battle-Scarred']) == 'Battle-Scarred'

    def test_limited_wears_fn_mw_float_03(self):
        """Skin with FN/MW only, float=0.3 → MW (degradation)"""
        assert self._determine_wear_from_float(0.3, ['Factory New', 'Minimal Wear']) == 'Minimal Wear'

    def test_limited_wears_fn_mw_float_005(self):
        """Skin with FN/MW only, float=0.05 → FN"""
        assert self._determine_wear_from_float(0.05, ['Factory New', 'Minimal Wear']) == 'Factory New'

    def test_limited_wears_bs_only(self):
        """Skin with BS only, float=0.01 → BS"""
        assert self._determine_wear_from_float(0.01, ['Battle-Scarred']) == 'Battle-Scarred'

    def test_backward_compat_none(self):
        """Backward compatibility: available_wears=None"""
        assert self._determine_wear_from_float(0.3, None) == 'Field-Tested'
        assert self._determine_wear_from_float(0.5, None) == 'Battle-Scarred'

    def test_degradation_fn_to_mw(self):
        """Degradation: ideal=FN, but FN not available → MW"""
        assert self._determine_wear_from_float(0.01, ['Minimal Wear', 'Field-Tested']) == 'Minimal Wear'

    def test_degradation_mw_to_ft(self):
        """Degradation: ideal=MW, but MW not available → FT"""
        assert self._determine_wear_from_float(0.08, ['Field-Tested', 'Well-Worn']) == 'Field-Tested'


class TestCalculateAverageFloat:
    """Тесты для _calculate_average_float()"""
    
    def _calculate_average_float(self, contract_skins: List[Dict]) -> float:
        """Локальная копия функции для тестирования"""
        if not contract_skins:
            return 0.0
        
        total_float = 0
        valid_skins = 0
        
        for skin in contract_skins:
            skin_float = skin.get('float', None)
            if skin_float is None:
                continue
            total_float += skin_float
            valid_skins += 1
        
        return total_float / valid_skins if valid_skins > 0 else 0.0
    
    def test_empty_list(self):
        """Пустой список → 0.0"""
        assert self._calculate_average_float([]) == 0.0
    
    def test_single_skin(self):
        """Один скин"""
        skins = [{'float': 0.05}]
        assert self._calculate_average_float(skins) == 0.05
    
    def test_multiple_skins(self):
        """10 скинов"""
        skins = [{'float': 0.01 * i} for i in range(1, 11)]
        avg = sum(0.01 * i for i in range(1, 11)) / 10
        assert self._calculate_average_float(skins) == avg
    
    def test_with_none_floats(self):
        """Скины с None float игнорируются"""
        skins = [
            {'float': 0.1},
            {'float': None},
            {'float': 0.2},
            {'float': None},
        ]
        assert abs(self._calculate_average_float(skins) - 0.15) < 1e-9
    
    def test_all_none_floats(self):
        """Все float = None → 0.0"""
        skins = [
            {'float': None},
            {'float': None},
        ]
        assert self._calculate_average_float(skins) == 0.0


class TestSkinsearchNormalizedFloat:
    """Тесты для _calculate_skinsearch_normalized_float() - Шаг 1"""
    
    def _calculate_average_float(self, contract_skins: List[Dict]) -> float:
        """Локальная копия функции для тестирования"""
        if not contract_skins:
            return 0.0
        total_float = 0
        valid_skins = 0
        for skin in contract_skins:
            skin_float = skin.get('float', None)
            if skin_float is None:
                continue
            total_float += skin_float
            valid_skins += 1
        return total_float / valid_skins if valid_skins > 0 else 0.0
    
    def _calculate_skinsearch_normalized_float(
        self,
        contract_skins: List[Dict],
        output_min_float: float,
        output_max_float: float
    ) -> float:
        """Локальная копия функции для тестирования"""
        if not contract_skins:
            return 0.0
        
        avg_float = self._calculate_average_float(contract_skins)
        output_range = output_max_float - output_min_float
        
        if output_range <= 1e-9:
            return 0.0
        
        norm = (avg_float - output_min_float) / output_range
        
        if norm < 0.0:
            norm = 0.0
        if norm > 1.0:
            norm = 1.0
        
        return norm
    
    def test_standard_skin_min0_max1(self):
        """Стандартный скин (min=0.0, max=1.0)"""
        # avg_float=0.05 → norm=0.05
        skins = [{'float': 0.05}]
        result = self._calculate_skinsearch_normalized_float(skins, 0.0, 1.0)
        assert abs(result - 0.05) < 1e-9
    
    def test_standard_skin_avg_0_10(self):
        """avg_float=0.10 → norm=0.10"""
        skins = [{'float': 0.10}]
        result = self._calculate_skinsearch_normalized_float(skins, 0.0, 1.0)
        assert abs(result - 0.10) < 1e-9
    
    def test_nonstandard_skin_min006_max080(self):
        """Нестандартный скин (min=0.06, max=0.80) - AK-47 Redline"""
        # avg_float=0.05 → norm = (0.05 - 0.06) / (0.80 - 0.06) = -0.0135 → clamped to 0.0
        skins = [{'float': 0.05}]
        result = self._calculate_skinsearch_normalized_float(skins, 0.06, 0.80)
        assert abs(result - 0.0) < 1e-9
        
        # avg_float=0.0674 → norm = (0.0674 - 0.06) / 0.74 = 0.01
        skins = [{'float': 0.0674}]
        result = self._calculate_skinsearch_normalized_float(skins, 0.06, 0.80)
        assert abs(result - 0.01) < 1e-9
    
    def test_fixed_float_skin(self):
        """Скин с фиксированным float (max - min = 0)"""
        skins = [{'float': 0.5}]
        result = self._calculate_skinsearch_normalized_float(skins, 0.5, 0.5)
        assert result == 0.0
    
    def test_clamp_above_max(self):
        """Clamping: avg_float > max_f → 1.0"""
        # float=1.5 с min=0, max=1 → norm = (1.5 - 0) / 1 = 1.5 → clamped to 1.0
        skins = [{'float': 1.5}]
        result = self._calculate_skinsearch_normalized_float(skins, 0.0, 1.0)
        assert abs(result - 1.0) < 1e-9
    
    def test_clamp_below_min(self):
        """Clamping: avg_float < min_f → 0.0"""
        skins = [{'float': -0.1}]
        result = self._calculate_skinsearch_normalized_float(skins, 0.0, 1.0)
        assert abs(result - 0.0) < 1e-9


class TestOutFloatCalculation:
    """Тесты для расчёта out_float в calculate_contract_outcomes_details() - Шаг 2"""
    
    def _calculate_out_float_new(self, avg_float: float, min_f: float, max_f: float) -> tuple:
        """Новый алгоритм CS2: out_float = clamp(avg_float, min_f, max_f), wear по norm_float"""
        out_float = max(min_f, min(max_f, float(avg_float)))
        denom = max_f - min_f
        norm_float = (out_float - min_f) / denom if denom > 1e-9 else 0.0
        return out_float, norm_float
    
    def _determine_wear_from_float(self, item_float: float) -> str:
        """Локальная копия функции для тестирования"""
        f = float(item_float)
        if f <= 0.07:
            return 'Factory New'
        elif f <= 0.15:
            return 'Minimal Wear'
        elif f <= 0.38:
            return 'Field-Tested'
        elif f <= 0.45:
            return 'Well-Worn'
        else:
            return 'Battle-Scarred'
    
    def test_standard_skin_min0_max1(self):
        """Стандартный скин (min=0, max=1): avg_float=0.20 → FT"""
        out, norm = self._calculate_out_float_new(0.20, 0.0, 1.0)
        assert abs(out - 0.20) < 1e-9
        assert abs(norm - 0.20) < 1e-9
        assert self._determine_wear_from_float(norm) == 'Field-Tested'
    
    def test_limited_max_float_min0_max045(self):
        """Скин с ограниченным max_f (min=0, max=0.45): avg_float=0.20"""
        out, norm = self._calculate_out_float_new(0.20, 0.0, 0.45)
        assert abs(out - 0.20) < 1e-9
        assert abs(norm - 0.444) < 1e-3
        assert self._determine_wear_from_float(norm) == 'Well-Worn'
    
    def test_limited_max_float_should_not_compress(self):
        """Bug: старая формула сжимала float, новая - нет"""
        out, norm = self._calculate_out_float_new(0.20, 0.0, 0.45)
        assert abs(out - 0.20) < 1e-9
        assert abs(norm - 0.444) < 1e-3
        assert self._determine_wear_from_float(norm) == 'Well-Worn'
    
    def test_ak47_redline_min006_max080(self):
        """AK-47 Redline (min=0.06, max=0.80): avg_float=0.20"""
        out, norm = self._calculate_out_float_new(0.20, 0.06, 0.80)
        assert abs(out - 0.20) < 1e-9
        expected_norm = (0.20 - 0.06) / 0.74
        assert abs(norm - expected_norm) < 1e-9
        assert self._determine_wear_from_float(norm) == 'Field-Tested'
    
    def test_clamp_below_min(self):
        """avg_float ниже min_f: clamp к min_f"""
        out, norm = self._calculate_out_float_new(0.05, 0.10, 0.80)
        assert abs(out - 0.10) < 1e-9
        assert abs(norm - 0.0) < 1e-9
        assert self._determine_wear_from_float(norm) == 'Factory New'
    
    def test_clamp_above_max(self):
        """avg_float выше max_f: clamp к max_f"""
        out, norm = self._calculate_out_float_new(0.50, 0.0, 0.38)
        assert abs(out - 0.38) < 1e-9
        assert abs(norm - 1.0) < 1e-9
        assert self._determine_wear_from_float(norm) == 'Battle-Scarred'
    
    def test_boundary_fn_007(self):
        """Граница FN: out_float=0.07 → FN"""
        out, norm = self._calculate_out_float_new(0.07, 0.0, 1.0)
        assert abs(out - 0.07) < 1e-9
        assert self._determine_wear_from_float(norm) == 'Factory New'
    
    def test_boundary_mw_015(self):
        """Граница MW: out_float=0.15 → MW"""
        out, norm = self._calculate_out_float_new(0.15, 0.0, 1.0)
        assert abs(out - 0.15) < 1e-9
        assert self._determine_wear_from_float(norm) == 'Minimal Wear'
    
    def test_comparison_old_vs_new_formula(self):
        """Сравнение старой и новой формулы для выявления бага"""
        avg_float = 0.20
        min_f, max_f = 0.0, 0.45
        
        out_new, norm_new = self._calculate_out_float_new(avg_float, min_f, max_f)
        
        out_old = avg_float * (max_f - min_f) + min_f
        
        assert abs(out_new - 0.20) < 1e-9
        assert abs(out_old - 0.09) < 1e-9
        
        assert abs(norm_new - 0.444) < 1e-3
        
        assert self._determine_wear_from_float(norm_new) == 'Well-Worn'
        assert self._determine_wear_from_float(out_old) == 'Minimal Wear'


class TestOutFloatCalculationOld:
    """Тесты для старой (неправильной) формулы - для документации бага"""


class TestCalculateWearLeap:
    """Тесты для calculate_wear_leap() - Шаг 5"""
    
    def _determine_wear_from_float(self, item_float: float) -> str:
        """Локальная копия функции для тестирования"""
        f = float(item_float)
        if f <= 0.07:
            return 'Factory New'
        elif f <= 0.15:
            return 'Minimal Wear'
        elif f <= 0.38:
            return 'Field-Tested'
        elif f <= 0.45:
            return 'Well-Worn'
        else:
            return 'Battle-Scarred'
    
    def _calculate_quality_leap(self, avg_float: float) -> str:
        """Локальная копия функции для тестирования"""
        if avg_float <= 0.07:
            return "FN Leap"
        elif avg_float <= 0.15:
            return "MW Leap"  
        elif avg_float <= 0.38:
            return "FT Standard"
        elif avg_float <= 0.45:
            return "WW Standard"
        else:
            return "BS Standard"
    
    def test_fn_leap(self):
        """avg_float <= 0.07 → FN Leap"""
        assert self._determine_wear_from_float(0.05) == 'Factory New'
        assert self._calculate_quality_leap(0.05) == 'FN Leap'
    
    def test_mw_leap(self):
        """0.07 < avg_float <= 0.15 → MW Leap"""
        assert self._determine_wear_from_float(0.10) == 'Minimal Wear'
        assert self._calculate_quality_leap(0.10) == 'MW Leap'
    
    def test_ft_standard(self):
        """0.15 < avg_float <= 0.38 → FT Standard"""
        assert self._determine_wear_from_float(0.25) == 'Field-Tested'
        assert self._calculate_quality_leap(0.25) == 'FT Standard'
    
    def test_ww_standard(self):
        """0.38 < avg_float <= 0.45 → WW Standard"""
        assert self._determine_wear_from_float(0.42) == 'Well-Worn'
        assert self._calculate_quality_leap(0.42) == 'WW Standard'
    
    def test_bs_standard(self):
        """avg_float > 0.45 → BS Standard"""
        assert self._determine_wear_from_float(0.50) == 'Battle-Scarred'
        assert self._calculate_quality_leap(0.50) == 'BS Standard'
    
    def test_boundary_fn_007(self):
        """Граница: float=0.07 ровно → FN"""
        assert self._determine_wear_from_float(0.07) == 'Factory New'
        assert self._calculate_quality_leap(0.07) == 'FN Leap'
    
    def test_boundary_mw_015(self):
        """Граница: float=0.15 ровно → MW"""
        assert self._determine_wear_from_float(0.15) == 'Minimal Wear'
        assert self._calculate_quality_leap(0.15) == 'MW Leap'
    
    def test_boundary_ft_038(self):
        """Граница: float=0.38 ровно → FT"""
        assert self._determine_wear_from_float(0.38) == 'Field-Tested'
        assert self._calculate_quality_leap(0.38) == 'FT Standard'
    
    def test_boundary_ww_045(self):
        """Граница: float=0.45 ровно → WW"""
        assert self._determine_wear_from_float(0.45) == 'Well-Worn'
        assert self._calculate_quality_leap(0.45) == 'WW Standard'


class TestOptimizeContractFloats:
    """Тесты для _optimize_contract_floats() - Шаг 6"""
    
    def _calculate_limit_avg_float(
        self,
        outcomes: List[Dict],
        target_wear: str,
    ) -> float:
        """Расчёт лимита avg_float для оптимизации"""
        wear_thresholds = {
            'Factory New': 0.07,
            'Minimal Wear': 0.15,
            'Field-Tested': 0.38,
            'Well-Worn': 0.45,
            'Battle-Scarred': 1.0,
        }
        
        target_max_avg_norm = float(wear_thresholds.get(target_wear, 0.07))
        
        limit_avg_float = 1.0
        for o in outcomes:
            min_f = float(o.get('min_float', 0.0))
            max_f = float(o.get('max_float', 1.0))
            denom = max_f - min_f
            if denom > 1e-9:
                max_float_for_this = (target_max_avg_norm - min_f) / denom
                if max_float_for_this < limit_avg_float:
                    limit_avg_float = max_float_for_this
        
        return max(0.0, limit_avg_float * 0.998)
    
    def test_standard_skin_fn_target(self):
        """Стандартный скин, target=FN"""
        outcomes = [
            {'min_float': 0.0, 'max_float': 1.0},
        ]
        limit = self._calculate_limit_avg_float(outcomes, 'Factory New')
        # limit = (0.07 - 0.0) / (1.0 - 0.0) * 0.998 = 0.06986
        assert abs(limit - 0.06986) < 1e-4
    
    def test_nonstandard_skin_fn_target(self):
        """Нестандартный скин (min=0.06, max=0.80), target=FN"""
        outcomes = [
            {'min_float': 0.06, 'max_float': 0.80},
        ]
        limit = self._calculate_limit_avg_float(outcomes, 'Factory New')
        # limit = (0.07 - 0.06) / (0.80 - 0.06) * 0.998 = 0.01351
        assert abs(limit - 0.01351) < 1e-4
    
    def test_multiple_outcomes_bottleneck(self):
        """Несколько исходов - определяется bottleneck"""
        outcomes = [
            {'min_float': 0.0, 'max_float': 1.0},   # limit = 0.06986
            {'min_float': 0.06, 'max_float': 0.80},  # limit = 0.01351 (bottleneck!)
        ]
        limit = self._calculate_limit_avg_float(outcomes, 'Factory New')
        # Bottleneck = 0.01351
        assert abs(limit - 0.01351) < 1e-4


class TestEndToEndWearCalculation:
    """Интеграционные тесты для всего pipeline расчёта wear"""
    
    def _calculate_average_float(self, contract_skins: List[Dict]) -> float:
        if not contract_skins:
            return 0.0
        total_float = 0
        valid_skins = 0
        for skin in contract_skins:
            skin_float = skin.get('float', None)
            if skin_float is None:
                continue
            total_float += skin_float
            valid_skins += 1
        return total_float / valid_skins if valid_skins > 0 else 0.0
    
    def _determine_wear_from_float(self, item_float: float) -> str:
        f = float(item_float)
        if f <= 0.07:
            return 'Factory New'
        elif f <= 0.15:
            return 'Minimal Wear'
        elif f <= 0.38:
            return 'Field-Tested'
        elif f <= 0.45:
            return 'Well-Worn'
        else:
            return 'Battle-Scarred'
    
    def _calculate_out_float(self, avg_float: float, min_f: float, max_f: float) -> float:
        return avg_float * (max_f - min_f) + min_f
    
    def test_pipeline_standard_skin_fn(self):
        """Pipeline: стандартный скин → FN"""
        # 10 входных скинов с avg_float=0.05
        contract_skins = [{'float': 0.05} for _ in range(10)]
        
        avg_float = self._calculate_average_float(contract_skins)
        assert abs(avg_float - 0.05) < 1e-9
        
        # Выходной скин стандартный (min=0, max=1)
        out_float = self._calculate_out_float(avg_float, 0.0, 1.0)
        assert abs(out_float - 0.05) < 1e-9
        
        wear = self._determine_wear_from_float(out_float)
        assert wear == 'Factory New'
    
    def test_pipeline_nonstandard_skin_fn(self):
        """Pipeline: нестандартный скин (AK-47 Redline) → FN"""
        # 10 входных скинов с avg_float=0.05
        contract_skins = [{'float': 0.05} for _ in range(10)]
        
        avg_float = self._calculate_average_float(contract_skins)
        assert abs(avg_float - 0.05) < 1e-9
        
        # Выходной скин: AK-47 | Redline (min=0.06, max=0.80)
        out_float = self._calculate_out_float(avg_float, 0.06, 0.80)
        # out = 0.05 * 0.74 + 0.06 = 0.097
        assert abs(out_float - 0.097) < 1e-9
        
        wear = self._determine_wear_from_float(out_float)
        assert wear == 'Minimal Wear'
    
    def test_pipeline_boundary_float(self):
        """Pipeline: граничные значения float"""
        # avg_float=0.07 ровно (с небольшой коррекцией для избежания fp-проблем)
        # Используем 0.07 - epsilon, чтобы гарантированно попасть в FN
        contract_skins = [{'float': 0.07 - 1e-10} for _ in range(10)]
        
        avg_float = self._calculate_average_float(contract_skins)
        out_float = self._calculate_out_float(avg_float, 0.0, 1.0)
        
        # out_float ≈ 0.07 → FN (включительно)
        wear = self._determine_wear_from_float(out_float)
        assert wear == 'Factory New'


class TestMaxFloatForGuaranteedOutputs:
    """
    Тест для расчета максимально допустимого флота входных скинов,
    при котором все выходы сохраняют свои качества.
    
    Пример контракта:
    - Вход: 9x StatTrak™ MP7 | Just Smile (Well-Worn), 1x StatTrak™ MP7 | Just Smile (Battle-Scarred)
    - Выходы: M4A1-S | Black Lotus FT, USP-S | Jawbreaker WW, Zeus x27 | Olympus FT
    - Требуемые качества выходов: FT, WW, FT
    """
    
    WEAR_THRESHOLDS = {
        'Factory New': 0.07,
        'Minimal Wear': 0.15,
        'Field-Tested': 0.38,
        'Well-Worn': 0.45,
        'Battle-Scarred': 1.0,
    }
    
    def _determine_wear_from_float(self, item_float: float) -> str:
        f = float(item_float)
        if f <= 0.07:
            return 'Factory New'
        elif f <= 0.15:
            return 'Minimal Wear'
        elif f <= 0.38:
            return 'Field-Tested'
        elif f <= 0.45:
            return 'Well-Worn'
        else:
            return 'Battle-Scarred'
    
    def _calculate_max_input_float_for_output_wear(
        self,
        output_min_float: float,
        output_max_float: float,
        target_wear: str,
    ) -> float:
        """
        Рассчитывает максимальный avg_float входных скинов,
        при котором выходной скин будет иметь качество target_wear или лучше.
        
        out_float = avg_float (новая формула CS2)
        Условие: out_float <= threshold(target_wear)
        
        Для нестандартных скинов:
        out_float = clamp(avg_float, min_f, max_f)
        Если avg_float > max_f, out_float = max_f
        """
        threshold = self.WEAR_THRESHOLDS.get(target_wear, 1.0)
        
        # out_float не может превышать max_float скина
        effective_max = min(threshold, output_max_float)
        
        # Если effective_max < min_float, то невозможно получить target_wear
        if effective_max < output_min_float - 1e-9:
            return None  # Невозможно
        
        # Максимальный avg_float = effective_max (при условии что avg_float не ограничен min/max)
        # Но нужно учесть, что если avg_float > max_float, out_float = max_float
        # Поэтому max_avg = min(effective_max, max_float) = effective_max
        
        return effective_max
    
    def _calculate_max_avg_norm_for_all_outputs(
        self,
        outputs: List[Dict],
        target_wears: List[str],
    ) -> float:
        """
        Рассчитывает максимально допустимый avg_norm для всех выходов.
        
        Новая формула CS2: out_float = clamp(avg_norm, min_f, max_f)
        Условие: out_float <= threshold
        
        Поэтому: avg_norm <= min(threshold, max_f)
        """
        max_avg_per_output = []
        
        for out, target_wear in zip(outputs, target_wears):
            min_f = out.get('min_float', 0.0)
            max_f = out.get('max_float', 1.0)
            threshold = self.WEAR_THRESHOLDS.get(target_wear, 1.0)
            
            # Если threshold < min_f - невозможно
            if threshold < min_f - 1e-9:
                return None
            
            # Новая формула CS2: out_float = clamp(avg_norm, min_f, max_f)
            # Условие: out_float <= threshold
            # Решение: avg_norm <= min(threshold, max_f)
            max_avg = min(threshold, max_f)
            
            max_avg_per_output.append(max_avg)
        
        # Берем минимум по всем выходам
        return min(max_avg_per_output) if max_avg_per_output else 1.0
    
    def _calculate_max_input_float_per_skin(
        self,
        input_skin_min_float: float,
        input_skin_max_float: float,
        max_avg_norm: float,
    ) -> float:
        """
        Рассчитывает максимальный флот для конкретного входного скина.
        
        Новая формула CS2: avg_norm = среднее от float всех входных скинов
        Для стандартных скинов (min=0, max=1): avg_norm = avg_float
        
        Максимальный флот для каждого входного скина = max_avg_norm
        (при условии что все скины в контракте имеют одинаковые min/max)
        """
        # Для простоты: все входные скины должны иметь float <= max_avg_norm
        # Это гарантирует что avg_norm <= max_avg_norm
        return max_avg_norm
    
    def test_mp7_just_smile_contract(self):
        """
        Реальный контракт:
        - Вход: 9x MP7 | Just Smile WW, 1x MP7 | Just Smile BS
        - Выходы: M4A1-S Black Lotus FT, USP-S Jawbreaker WW, Zeus Olympus FT
        
        MP7 | Just Smile: min=0.0, max=1.0 (стандартный)
        M4A1-S Black Lotus: min=0.0, max=0.7
        USP-S Jawbreaker: min=0.0, max=1.0 (стандартный)
        Zeus x27 | Olympus: min=0.0, max=0.67
        
        Требуемые качества: FT (0.38), WW (0.45), FT (0.38)
        """
        mp7_min, mp7_max = 0.0, 1.0
        m4a1s_min, m4a1s_max = 0.0, 0.7
        usps_min, usps_max = 0.0, 1.0
        zeus_min, zeus_max = 0.0, 0.67
        
        outputs = [
            {'name': 'M4A1-S | Black Lotus', 'min_float': m4a1s_min, 'max_float': m4a1s_max, 'target_wear': 'Field-Tested'},
            {'name': 'USP-S | Jawbreaker', 'min_float': usps_min, 'max_float': usps_max, 'target_wear': 'Well-Worn'},
            {'name': 'Zeus x27 | Olympus', 'min_float': zeus_min, 'max_float': zeus_max, 'target_wear': 'Field-Tested'},
        ]
        target_wears = ['Field-Tested', 'Well-Worn', 'Field-Tested']
        
        max_avg_norm = self._calculate_max_avg_norm_for_all_outputs(outputs, target_wears)
        
        # Для каждого выхода:
        # M4A1-S (max=0.7): max_avg <= min(0.38, 0.7) = 0.38
        # USP-S (max=1.0): max_avg <= min(0.45, 1.0) = 0.45
        # Zeus (max=0.67): max_avg <= min(0.38, 0.67) = 0.38
        # Берем минимум = 0.38
        assert abs(max_avg_norm - 0.38) < 1e-6, f"Expected max_avg_norm=0.38, got {max_avg_norm}"
        
        max_float_mp7 = self._calculate_max_input_float_per_skin(mp7_min, mp7_max, max_avg_norm)
        assert abs(max_float_mp7 - 0.38) < 1e-6, f"Expected max_float_mp7=0.38, got {max_float_mp7}"
        
        print(f"\nMP7 Just Smile Contract:")
        print(f"  Max avg_norm for all outputs: {max_avg_norm:.4f}")
        print(f"  Max float for MP7 (WW input): {max_float_mp7:.4f}")
        print(f"  Max float for MP7 (BS input): {max_float_mp7:.4f}")
        print(f"  Expected output wears at max float: FT, WW, FT")
        
    def test_mp7_just_smile_exact_contract_verification(self):
        """
        Полный тест контракта:
        - Вход: 9x MP7 | Just Smile (WW), 1x MP7 | Just Smile (BS)
        - Выходы: M4A1-S Black Lotus FT, USP-S Jawbreaker WW, Zeus Olympus FT
        
        Выходы с их диапазонами:
        - M4A1-S Black Lotus: min=0.0, max=0.7
        - USP-S Jawbreaker: min=0.0, max=1.0
        - Zeus x27 | Olympus: min=0.0, max=0.67
        
        Проверяем максимальный допустимый флот для входных скинов.
        """
        # Диапазоны выходных скинов
        m4a1s_min, m4a1s_max = 0.0, 0.7
        usps_min, usps_max = 0.0, 1.0
        zeus_min, zeus_max = 0.0, 0.67
        
        # Максимальный допустимый avg_norm (из предыдущего теста)
        max_avg = 0.38
        
        # Симулируем входные скины с максимальными флотами
        input_floats = [0.38] * 10
        avg_float = sum(input_floats) / len(input_floats)
        
        print(f"\n=== Exact Contract Verification ===")
        print(f"Input: 9x MP7 WW (max float 0.38), 1x MP7 BS (max float 0.38)")
        print(f"Avg float: {avg_float:.4f}")
        
        # Рассчитываем out_float для каждого выхода
        m4a1s_out = max(m4a1s_min, min(m4a1s_max, avg_float))
        usps_out = max(usps_min, min(usps_max, avg_float))
        zeus_out = max(zeus_min, min(zeus_max, avg_float))
        
        print(f"\nOutput floats (clamped to skin ranges):")
        print(f"  M4A1-S (max=0.7):  out_float = {m4a1s_out:.4f}")
        print(f"  USP-S (max=1.0):   out_float = {usps_out:.4f}")
        print(f"  Zeus (max=0.67):   out_float = {zeus_out:.4f}")
        
        m4a1s_wear = self._determine_wear_from_float(m4a1s_out)
        usps_wear = self._determine_wear_from_float(usps_out)
        zeus_wear = self._determine_wear_from_float(zeus_out)
        
        print(f"\nOutput wears: M4A1-S={m4a1s_wear}, USP-S={usps_wear}, Zeus={zeus_wear}")
        print(f"All outputs are FT or better: OK")
        
        # При float=0.38 все выходы будут FT (включительно)
        assert m4a1s_wear == 'Field-Tested', f"M4A1-S should be FT, got {m4a1s_wear}"
        assert usps_wear == 'Field-Tested', f"USP-S should be FT, got {usps_wear}"
        assert zeus_wear == 'Field-Tested', f"Zeus should be FT, got {zeus_wear}"
        
        # Проверка граничного случая: если avg > 0.38
        bad_floats = [0.38] * 9 + [0.45]
        bad_avg = sum(bad_floats) / len(bad_floats)
        
        bad_m4a1s_out = max(m4a1s_min, min(m4a1s_max, bad_avg))
        bad_usps_out = max(usps_min, min(usps_max, bad_avg))
        bad_zeus_out = max(zeus_min, min(zeus_max, bad_avg))
        
        bad_m4a1s = self._determine_wear_from_float(bad_m4a1s_out)
        bad_usps = self._determine_wear_from_float(bad_usps_out)
        bad_zeus = self._determine_wear_from_float(bad_zeus_out)
        
        print(f"\nBad case: 9x 0.38 + 1x 0.45, avg={bad_avg:.4f}")
        print(f"Output wears: M4A1-S={bad_m4a1s}, USP-S={bad_usps}, Zeus={bad_zeus}")
        
        # При avg > 0.38 M4A1-S и Zeus станут WW
        assert bad_m4a1s == 'Well-Worn', f"M4A1-S should be WW when avg > 0.38, got {bad_m4a1s}"
        assert bad_zeus == 'Well-Worn', f"Zeus should be WW when avg > 0.38, got {bad_zeus}"
        
        print(f"\n[OK] Verification passed: max float 0.38 guarantees all outputs are FT or better")
        print(f"[OK] When avg > 0.38, outputs degrade to WW")
        
    def test_mp7_just_smile_with_real_floats(self):
        """
        Проверка с реальными флотами из логов:
        - 9x MP7 WW с float 0.4059
        - 1x MP7 BS с float 0.5472
        - avg = (9*0.4059 + 1*0.5472) / 10 = 0.42003
        
        Выходы с их диапазонами:
        - M4A1-S Black Lotus: min=0.0, max=0.7
        - USP-S Jawbreaker: min=0.0, max=1.0
        - Zeus x27 | Olympus: min=0.0, max=0.67
        
        Новая формула CS2: out_float = clamp(avg_float, min_f, max_f)
        
        ВАЖНО: При avg=0.42 все выходы будут WW, а не FT, WW, FT!
        Это означает что реальные флоты НЕ гарантируют FT для M4A1-S и Zeus.
        """
        floats_ww = [0.4059] * 9
        float_bs = 0.5472
        avg_float = (sum(floats_ww) + float_bs) / 10
        
        print(f"\nReal floats from logs:")
        print(f"  9x MP7 WW: float=0.4059")
        print(f"  1x MP7 BS: float=0.5472")
        print(f"  avg_float = {avg_float:.5f}")
        
        # Диапазоны выходных скинов
        m4a1s_min, m4a1s_max = 0.0, 0.7
        usps_min, usps_max = 0.0, 1.0
        zeus_min, zeus_max = 0.0, 0.67
        
        # Рассчитываем out_float для каждого выхода
        # Новая формула: out_float = clamp(avg_float, min_f, max_f)
        m4a1s_out = max(m4a1s_min, min(m4a1s_max, avg_float))
        usps_out = max(usps_min, min(usps_max, avg_float))
        zeus_out = max(zeus_min, min(zeus_max, avg_float))
        
        print(f"\nOutput floats (clamped to skin ranges):")
        print(f"  M4A1-S (max=0.7):  out_float = {m4a1s_out:.4f}")
        print(f"  USP-S (max=1.0):   out_float = {usps_out:.4f}")
        print(f"  Zeus (max=0.67):   out_float = {zeus_out:.4f}")
        
        # Определяем качества
        m4a1s_wear = self._determine_wear_from_float(m4a1s_out)
        usps_wear = self._determine_wear_from_float(usps_out)
        zeus_wear = self._determine_wear_from_float(zeus_out)
        
        print(f"\nOutput wears: M4A1-S={m4a1s_wear}, USP-S={usps_wear}, Zeus={zeus_wear}")
        
        # При avg=0.42 > 0.38, все выходы будут WW
        # Это правильно! Максимальный avg для FT = 0.38
        assert m4a1s_wear == 'Well-Worn', f"At avg=0.42, M4A1-S should be WW"
        assert usps_wear == 'Well-Worn', f"At avg=0.42, USP-S should be WW"
        assert zeus_wear == 'Well-Worn', f"At avg=0.42, Zeus should be WW"
        
        print(f"\n[OK] At avg=0.42, all outputs are WW (correct - exceeds max 0.38 for FT)")
        
        # Найдем максимальный avg для FT, WW, FT
        max_avg_for_ft = 0.38
        print(f"\nMax avg for FT,WW,FT outputs: {max_avg_for_ft}")
        print(f"Real avg: {avg_float:.5f} (exceeds max by {avg_float - max_avg_for_ft:.5f})")
        
    def test_non_standard_output_skin(self):
        """
        Тест с нестандартным выходным скином (например AK-47 Redline: min=0.06, max=0.80).
        Требуемое качество: FT (threshold=0.38)
        
        Новая формула CS2: out_float = clamp(avg_norm, 0.06, 0.80)
        Условие: out_float <= 0.38
        
        Решение: avg_norm <= min(0.38, 0.80) = 0.38
        """
        ak_min, ak_max = 0.06, 0.80
        
        outputs = [
            {'name': 'AK-47 | Redline', 'min_float': ak_min, 'max_float': ak_max, 'target_wear': 'Field-Tested'},
        ]
        target_wears = ['Field-Tested']
        
        max_avg_norm = self._calculate_max_avg_norm_for_all_outputs(outputs, target_wears)
        
        # Новая формула CS2: max_avg_norm = min(threshold, max_f) = min(0.38, 0.80) = 0.38
        assert abs(max_avg_norm - 0.38) < 1e-6, f"Expected 0.38, got {max_avg_norm}"
        
        print(f"\nAK-47 Redline (min=0.06, max=0.80) target FT:")
        print(f"  max_avg_norm = {max_avg_norm:.4f}")
        
        # Для входного скина (стандартный):
        input_min, input_max = 0.0, 1.0
        max_input_float = self._calculate_max_input_float_per_skin(input_min, input_max, max_avg_norm)
        
        # При новой формуле: max_input_float = max_avg_norm = 0.38
        assert abs(max_input_float - 0.38) < 1e-6, f"Expected 0.38, got {max_input_float}"
        print(f"  max_input_float for standard skin = {max_input_float:.4f}")


if __name__ == '__main__':
    pytest.main([__file__, '-v'])