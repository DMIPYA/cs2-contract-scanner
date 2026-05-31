from typing import List, Dict, Optional, Tuple
import threading
import time
import os
from dataclasses import dataclass
from collections import defaultdict
import logging
import collections
import itertools
import math
import random
from collections import Counter
import statistics
from database import CS2Database, SkinData
from api_client import PriceManager
from calculator_price_lookup import _PriceLookupMixin


@dataclass
class ContractResult:
    """Результат расчета контракта"""
    target_skin: str
    probability: float
    investment_cost: float
    expected_value: float
    roi_percentage: float
    collection_name: str
    input_skins: List[str]
    max_average_float: float = 0.0
    wear_leap_info: Optional[Dict] = None


@dataclass
class SkinProbability:
    """Вероятность выпадения скина"""
    skin_name: str
    probability: float
    collection: str
    rarity: str


class ContractCalculator(_PriceLookupMixin):
    """Калькулятор контрактов CS2 с реальными данными"""
    
    def __init__(self, database: CS2Database, price_manager: PriceManager):
        self.database = database
        self.price_manager = price_manager

        self._logger = logging.getLogger('calculator')
        self._logger.propagate = True

        self._cross_contracts_cache_lock = threading.Lock()
        self._cross_contracts_cache: Optional[List[Dict]] = None
        self._cross_contracts_cache_ts: float = 0.0
        self._cross_contracts_cache_refreshing: bool = False
        self._cross_contracts_cache_refresh_started_ts: float = 0.0
        self._cross_contracts_cache_last_success_ts: float = 0.0
        self._cross_contracts_cache_last_duration_seconds: float = 0.0
        self._cross_contracts_cache_last_error: str = ""
        self._cross_contracts_cache_ttl_seconds: int = 600

        self._memo_price_with_float: Dict[Tuple, Optional[Tuple[float, float, str]]] = {}
        self._memo_price: Dict[Tuple, Optional[float]] = {}
        self._memo_possible_outputs: Dict[Tuple, List[Dict]] = {}
        self._memo_next_grade_count: Dict[Tuple, int] = {}
        self._memo_main_skins: Dict[Tuple, List[Dict]] = {}
        self._memo_collection_imbalance: Dict[Tuple, Optional[float]] = {}
        self._memo_target_rank: Dict[Tuple, List[Dict]] = {}
        self._memo_contract_target_prob: Dict[Tuple, float] = {}
        self._memo_contract_max_output: Dict[Tuple, float] = {}
        self._memo_contract_craftability: Dict[Tuple, Dict] = {}

        # Гранулярные блокировки для memo-словарей (см. optimization-roadmap.md Step 2)
        # При добавлении нового memo-словаря — добавить lock в соответствующую группу
        self._lock_listings = threading.Lock()          # _memo_listings
        self._lock_sell_price = threading.Lock()        # _memo_effective_sell_price
        self._lock_price_float = threading.Lock()       # _memo_price_with_float, _memo_price
        self._lock_main_skins = threading.Lock()        # _memo_main_skins
        self._lock_next_grade = threading.Lock()        # _memo_next_grade_count, _memo_possible_outputs, _memo_collection_imbalance, _memo_collection_avg_outcome_price, _memo_collection_score
        self._lock_contract_eval = threading.Lock()     # _memo_contract_eval, _memo_contract_craftability, _memo_contract_target_prob, _memo_contract_max_output, _memo_target_rank

        self._last_target_suite_diagnostics: Optional[Dict] = None
        self.rarities_hierarchy = {
            "Consumer": 0,
            "Industrial": 1, 
            "Mil-Spec": 2,
            "Restricted": 3,
            "Classified": 4,
            "Covert": 5
        }
        
        # Float пороги для определения wear (CS2 стандарт)
        # Factory New: 0.00 - 0.07
        # Minimal Wear: 0.07 - 0.15
        # Field-Tested: 0.15 - 0.38
        # Well-Worn: 0.38 - 0.45
        # Battle-Scarred: 0.45 - 1.00
        self.float_thresholds = {
            "Factory New": 0.07,
            "Minimal Wear": 0.15,
            "Field-Tested": 0.38,
            "Well-Worn": 0.45,
            "Battle-Scarred": 1.0
        }
        
        # Комиссия рынка (market.csgo.com берёт ~7%, не 15% как Steam Market)
        try:
            self.market_fee = float(os.getenv('MARKET_SELL_FEE', '0.07') or 0.07)
        except Exception:
            self.market_fee = 0.07

        try:
            self.csfloat_fee = float(os.getenv('CSFLOAT_FEE', '0.02') or 0.02)
        except Exception:
            self.csfloat_fee = 0.02

        self._multisource_net_pricing = False
        self._strict_input_float = str(os.getenv('STRICT_INPUT_FLOAT', '1') or '1').strip().lower() not in {'0', 'false', 'no', 'off'}

        self._output_multiplier_threshold = 2.5
        self._filler_to_target_price_ratio = 0.75

        self._max_risk_ratio = 10.0
        self._risk_ratio_override_min_roi = 60.0
        self._risk_ratio_override_min_profit_probability = 0.25
        self._golden_filler_price_multiplier = 3.0
        self._golden_filler_min_outcomes = 3

        self._max_worst_case_loss_pct = 0.50

        self._memo_collection_score: Dict[Tuple, Optional[float]] = {}
        self._memo_collection_avg_outcome_price: Dict[Tuple, Optional[float]] = {}
        self._memo_contract_eval: 'collections.OrderedDict[Tuple, Dict]' = collections.OrderedDict()
        try:
            self._memo_contract_eval_max = int(os.getenv('MEMO_CONTRACT_EVAL_MAX', '20000') or 20000)
        except Exception:
            self._memo_contract_eval_max = 20000

        self._memo_listings: Dict[Tuple, List[Tuple[float, Optional[float], str]]] = {}
        self._memo_effective_sell_price: Dict[Tuple, Optional[float]] = {}

    def clear_outcomes_cache(self):
        """Clear all cached outcomes data (wear labels may be stale)."""
        with self._lock_contract_eval:
            self._memo_contract_eval.clear()
            self._memo_contract_target_prob.clear()
            self._memo_contract_max_output.clear()
        self._logger.info('Outcomes cache cleared (wear calculation fix)')

    def get_last_target_suite_diagnostics(self) -> Optional[Dict]:
        return self._last_target_suite_diagnostics

    def _wear_to_max_float(self, wear: str) -> float:
        """Возвращает максимальное значение float для данного wear.
        
        CS2 wear ranges:
        - Factory New: 0.00 - 0.07
        - Minimal Wear: 0.07 - 0.15
        - Field-Tested: 0.15 - 0.38
        - Well-Worn: 0.38 - 0.45
        - Battle-Scarred: 0.45 - 1.00
        """
        w = str(wear or '')
        if w == 'Factory New':
            return 0.07
        if w == 'Minimal Wear':
            return 0.15
        if w == 'Field-Tested':
            return 0.38
        if w == 'Well-Worn':
            return 0.45
        return 1.0

    def _avg_outcome_price_for_collection(
        self,
        collection_name: str,
        input_rarity: str,
        *,
        is_stattrak: bool,
        target_wear: str,
    ) -> Optional[float]:
        input_rarity = self._normalize_rarity(input_rarity)
        key = (collection_name, input_rarity, bool(is_stattrak), str(target_wear))
        with self._lock_next_grade:
            if key in self._memo_collection_avg_outcome_price:
                return self._memo_collection_avg_outcome_price[key]

        outs = self._get_possible_outputs(collection_name, input_rarity, average_float=None, is_stattrak=is_stattrak)
        if not outs:
            with self._lock_next_grade:
                self._memo_collection_avg_outcome_price[key] = None
            return None

        prices = []
        for o in outs:
            p = self._cached_get_price(
                o['name'],
                target_wear=target_wear,
                exclude_stattrak=not is_stattrak,
                require_stattrak=bool(is_stattrak),
                strict_name_match=False,
                allow_refresh=False,
            )
            if p and float(p) > 0:
                prices.append(float(p))

        if not prices:
            with self._lock_next_grade:
                self._memo_collection_avg_outcome_price[key] = None
            return None

        val = float(sum(prices)) / float(len(prices))
        with self._lock_next_grade:
            self._memo_collection_avg_outcome_price[key] = float(val)
        return float(val)

    def _collection_score(self, collection_name: str, input_rarity: str, *, is_stattrak: bool) -> Optional[float]:
        input_rarity = self._normalize_rarity(input_rarity)
        key = (collection_name, input_rarity, bool(is_stattrak))
        with self._lock_next_grade:
            if key in self._memo_collection_score:
                return self._memo_collection_score[key]

        prices = []
        for w in ['Factory New', 'Minimal Wear', 'Field-Tested']:
            p = self._avg_outcome_price_for_collection(collection_name, input_rarity, is_stattrak=is_stattrak, target_wear=w)
            if p and float(p) > 0:
                prices.append(float(p))
        if not prices:
            with self._lock_next_grade:
                self._memo_collection_score[key] = None
            return None
        score = max(prices)
        with self._lock_next_grade:
            self._memo_collection_score[key] = float(score)
        return float(score)

    def _best_target_wear(self, collection_name: str, input_rarity: str, *, is_stattrak: bool) -> str:
        best_wear = 'Field-Tested'
        best_score = -1.0
        for w in ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']:
            out_price = self._avg_outcome_price_for_collection(
                collection_name, input_rarity, is_stattrak=is_stattrak, target_wear=w
            )
            if not out_price or float(out_price) <= 0:
                continue
            # Оцениваем цену входных скинов при правильном ограничении float
            # (т.е. сравниваем FN-выход/FN-вход, FT-выход/FT-вход — «яблоки к яблокам»).
            max_f = self._wear_to_max_float(w)
            in_skins = self._get_main_skins(
                collection_name,
                count=1,
                is_stattrak=is_stattrak,
                rarity=input_rarity,
                max_float=max_f,
            )
            if not in_skins:
                continue
            in_price = float(in_skins[0].get('price') or 0.0)
            if in_price <= 0:
                continue
            # Score = EV-ratio: средняя цена выхода / стоимость 1 входного скина
            # Вероятность одна и та же для всех wear → можно опустить.
            score = float(out_price) / float(in_price)
            if score > best_score:
                best_score = float(score)
                best_wear = w
        return best_wear

    def _get_candidate_inputs(
        self,
        collection_name: str,
        input_rarity: str,
        *,
        is_stattrak: bool,
        max_float: float,
        limit: int,
    ) -> List[Dict]:
        lim = int(max(1, limit))

        skins = self._get_main_skins(
            collection_name,
            count=int(max(1, lim)),
            is_stattrak=is_stattrak,
            rarity=input_rarity,
            max_float=float(max_float),
        )
        expanded = self._expand_items_with_unique_listings(
            list(skins),
            limit=int(lim),
            is_stattrak=is_stattrak,
            max_float=float(max_float),
        )

        lim2 = max(5, int(lim // 2))
        skins2 = self._get_main_skins(
            collection_name,
            count=int(max(1, lim2)),
            is_stattrak=is_stattrak,
            rarity=input_rarity,
            max_float=1.0,
        )
        expanded2 = self._expand_items_with_unique_listings(
            list(skins2),
            limit=int(lim2),
            is_stattrak=is_stattrak,
            max_float=1.0,
        )

        merged: List[Dict] = []
        seen = set()
        for it in list(expanded) + list(expanded2):
            try:
                key = str(it.get('instance_key') or '')
            except Exception:
                key = ''
            if not key:
                key = (
                    str(it.get('name') or ''),
                    round(float(it.get('price') or 0.0), 4),
                    round(float(it.get('float') or 0.0), 5),
                    str(it.get('wear') or ''),
                )
            if key in seen:
                continue
            seen.add(key)
            merged.append(it)
            if len(merged) >= int(lim):
                break

        # Debug aid: if a specific skin name is requested via HUNT_DEBUG_EVAL_* filters,
        # ensure it can show up in candidate inputs even if it is not among the cheapest.
        try:
            dbg = str(os.getenv('HUNT_DEBUG', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
            dbg_eval_coll = str(os.getenv('HUNT_DEBUG_EVAL_COLL', '') or '').strip().lower()
            dbg_eval_name = str(os.getenv('HUNT_DEBUG_EVAL_NAME', '') or '').strip().lower()
            if dbg and dbg_eval_name:
                coll_ok = (not dbg_eval_coll) or (dbg_eval_coll in str(collection_name or '').lower())
                if coll_ok:
                    has_nm = any(dbg_eval_name in str(x.get('name') or '').lower() for x in merged)
                    if not has_nm:
                        rarity_norm = self._normalize_rarity(input_rarity)
                        candidates = []
                        for s in self.database.get_collection_skins(collection_name):
                            if self._normalize_rarity(s.rarity) != rarity_norm:
                                continue
                            nm = str(getattr(s, 'name', '') or '')
                            if dbg_eval_name in nm.lower():
                                candidates.append(nm)
                        added_any = False
                        for nm in candidates[:3]:
                            pi = self._cached_get_price_with_float(
                                nm,
                                target_wear=None,
                                max_float=1.0,
                                exclude_stattrak=not is_stattrak,
                                require_stattrak=bool(is_stattrak),
                                strict_name_match=False,
                                allow_refresh=False,
                            )
                            if not pi:
                                continue
                            price, skin_float, wear = pi
                    self._logger.info(
                        'HuntDebugInputs coll=%s rarity=%s ST=%s want_nm=%s present_before=%s candidates=%s added=%s',
                        str(collection_name),
                        str(rarity_norm),
                        'Y' if bool(is_stattrak) else 'N',
                        str(os.getenv('HUNT_DEBUG_EVAL_NAME', '') or ''),
                        'Y' if bool(has_nm) else 'N',
                        str(candidates[:5]),
                        'Y' if bool(added_any) else 'N',
                    )
        except Exception:
            pass
        return list(merged)

    def _cached_get_listings(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = False,
        limit: int = 80,
    ) -> List[Tuple[float, Optional[float], str]]:
        mf = None if max_float is None else round(float(max_float), 4)
        key = (skin_name, target_wear, mf, bool(exclude_stattrak), bool(require_stattrak), bool(strict_name_match), bool(allow_refresh), int(limit))
        with self._lock_listings:
            cached = self._memo_listings.get(key)
        if cached is not None:
            return list(cached)

        lots = self.price_manager.get_listings(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
            limit=limit,
        )
        with self._lock_listings:
            self._memo_listings[key] = list(lots)
        return list(lots)

    def _cached_get_effective_sell_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = False,
    ) -> Optional[float]:
        mf = None if max_float is None else round(float(max_float), 4)
        key = (skin_name, target_wear, mf, bool(exclude_stattrak), bool(require_stattrak), bool(strict_name_match), bool(allow_refresh))
        with self._lock_sell_price:
            cached = self._memo_effective_sell_price.get(key)
        if cached is not None:
            return cached

        # If allow_refresh=False and we have no cached sales history, illiquid skins can be wildly overpriced
        # by orderbook manipulation. To mitigate: when listings look suspicious and env allows it,
        # do a one-off refresh for sales history.
        allow_refresh2 = bool(allow_refresh)
        try:
            if not allow_refresh2:
                metrics = self.price_manager.get_liquidity_metrics(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    strict_name_match=strict_name_match,
                    allow_refresh=False,
                    depth_n=10,
                )
                n = int(metrics.get('listings_count') or 0)
                p10 = metrics.get('p10_price')
                mn = metrics.get('min_price')
                base_book = float(p10) if p10 is not None else (float(mn) if mn is not None else None)

                try:
                    max_listings_for_sales = int(os.getenv('LIQ_MAX_LISTINGS_FOR_SALES', '3') or 3)
                except Exception:
                    max_listings_for_sales = 3
                try:
                    high_price_for_sales = float(os.getenv('LIQ_HIGH_PRICE_FOR_SALES', '250') or 250)
                except Exception:
                    high_price_for_sales = 250.0

                suspicious = (
                    (n > 0 and n <= int(max_listings_for_sales))
                    or (base_book is not None and float(base_book) >= float(high_price_for_sales) and n <= 10)
                )
                if suspicious and str(os.getenv('EFFECTIVE_SELL_ALLOW_SALES_REFRESH', '1')).strip() not in {'0', 'false', 'False'}:
                    allow_refresh2 = True
        except Exception:
            allow_refresh2 = bool(allow_refresh)

        val = self.price_manager.get_effective_sell_price(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh2,
        )
        val2 = float(val) if val is not None else None
        with self._lock_sell_price:
            self._memo_effective_sell_price[key] = val2
        return val2

    def _expand_items_with_unique_listings(
        self,
        base_items: List[Dict],
        *,
        limit: int,
        is_stattrak: bool,
        max_float: float,
    ) -> List[Dict]:
        if not base_items:
            return []
        if int(limit) <= 0:
            return []

        expanded: List[Dict] = []
        seen_instances = set()

        per_skin_listing_limit = max(40, int(limit) * 3)

        for it in base_items:
            if len(expanded) >= int(limit):
                break
            name = it.get('name')
            if not name:
                continue

            lots = self._cached_get_listings(
                str(name),
                target_wear=None,
                max_float=float(max_float) if max_float is not None else None,
                exclude_stattrak=not bool(is_stattrak),
                require_stattrak=bool(is_stattrak),
                strict_name_match=False,
                allow_refresh=False,
                limit=int(per_skin_listing_limit),
            )
            if not lots:
                # Fallback: keep the base item (even if it represents an aggregated/unknown lot)
                key = (
                    str(it.get('name')),
                    round(float(it.get('price') or 0.0), 4),
                    round(float(it.get('float') or 0.0), 5),
                    str(it.get('wear') or ''),
                )
                if key not in seen_instances:
                    seen_instances.add(key)
                    expanded.append(dict(it))
                continue

        for price, lot_float, wear in lots:
            if len(expanded) >= int(limit):
                break
            if lot_float is None:
                continue
            if max_float is not None and float(lot_float) > float(max_float):
                continue

            d = dict(it)
            d['price'] = float(price)
            d['float'] = float(lot_float)
            d['wear'] = str(wear)
            d['instance_key'] = f"{name}|{float(price):.4f}|{float(lot_float):.5f}|{wear}"
            expanded.append(d)

        return expanded[: int(limit)]

    def _evaluate_contract_cached(self, contract_skins: List[Dict], target_collection: str, *, is_stattrak: bool) -> Dict:
        key = (
            tuple(
                sorted(
                    (
                        s.get('name'),
                        s.get('collection'),
                        round(float(s.get('float') or 0.0), 4),
                        round(float(s.get('price') or 0.0), 4),
                    )
                    for s in contract_skins
                )
            ),
            target_collection,
            bool(is_stattrak),
        )
        with self._lock_contract_eval:
            cached = self._memo_contract_eval.get(key)
            if cached is not None:
                try:
                    self._memo_contract_eval.move_to_end(key)
                except Exception:
                    pass
        if cached is not None:
            return dict(cached)
        val = self._calculate_contract_profit(contract_skins, target_collection, is_stattrak=is_stattrak)
        with self._lock_contract_eval:
            try:
                self._memo_contract_eval[key] = dict(val)
                try:
                    self._memo_contract_eval.move_to_end(key)
                except Exception:
                    pass
                maxn = int(self._memo_contract_eval_max) if int(self._memo_contract_eval_max) > 0 else 0
                if maxn > 0:
                    while len(self._memo_contract_eval) > maxn:
                        try:
                            self._memo_contract_eval.popitem(last=False)
                        except Exception:
                            break
            except MemoryError:
                try:
                    self._memo_contract_eval.clear()
                except Exception:
                    pass
        return dict(val)

    def _check_contract_craftability(
        self,
        contract_skins: List[Dict],
        *,
        is_stattrak: bool,
        allow_unknown_float: bool = False,
        allow_refresh: bool = False,
    ) -> Dict:
        key = (
            tuple(
                sorted(
                    (
                        str(s.get('name') or ''),
                        round(float(s.get('float') or 0.0), 5),
                        str(s.get('wear') or ''),
                        str(s.get('instance_key') or ''),
                    )
                    for s in contract_skins
                )
            ),
            bool(is_stattrak),
            bool(allow_unknown_float),
            bool(allow_refresh),
        )
        with self._lock_contract_eval:
            cached = self._memo_contract_craftability.get(key)
        if cached is not None:
            return dict(cached)

        if not contract_skins:
            result = {
                'craftable': False,
                'reason': 'empty_contract',
                'min_depth': 0,
                'limiting_skin': None,
                'required_slots': 0,
                'matched_slots': 0,
            }
            with self._lock_contract_eval:
                self._memo_contract_craftability[key] = dict(result)
            return dict(result)

        try:
            per_skin_limit_mult = int(os.getenv('HUNT_CRAFTABILITY_FETCH_LIMIT_MULT', '6') or 6)
        except Exception:
            per_skin_limit_mult = 6
        if per_skin_limit_mult < 2:
            per_skin_limit_mult = 2

        required_by_skin: Dict[str, List[float]] = defaultdict(list)
        for s in contract_skins:
            skin_name = str(s.get('name') or '')
            if not skin_name:
                result = {
                    'craftable': False,
                    'reason': 'missing_skin_name',
                    'min_depth': 0,
                    'limiting_skin': None,
                    'required_slots': 0,
                    'matched_slots': 0,
                }
                with self._lock_contract_eval:
                    self._memo_contract_craftability[key] = dict(result)
                return dict(result)

        cap = s.get('float')
        try:
            cap_f = float(cap) if cap is not None else None
        except Exception:
            cap_f = None
        if cap_f is None:
            if not bool(allow_unknown_float):
                result = {
                    'craftable': False,
                    'reason': 'unknown_required_float',
                    'min_depth': 0,
                    'limiting_skin': skin_name,
                    'required_slots': 1,
                    'matched_slots': 0,
                }
                with self._lock_contract_eval:
                    self._memo_contract_craftability[key] = dict(result)
                return dict(result)
            cap_f = 1.0
        required_by_skin[skin_name].append(float(cap_f))

        min_depth = None
        limiting_skin = None
        limiting_required = 0
        limiting_matched = 0

        for skin_name, required_caps in required_by_skin.items():
            caps_sorted = sorted(float(x) for x in required_caps)
            fetch_limit = max(40, int(len(caps_sorted)) * int(per_skin_limit_mult))
            max_cap = max(caps_sorted) if caps_sorted else None

            try:
                lots = self._cached_get_listings(
                    skin_name,
                    target_wear=None,
                    max_float=float(max_cap) if max_cap is not None and float(max_cap) < 0.999999 else None,
                    exclude_stattrak=not bool(is_stattrak),
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=False,
                    allow_refresh=bool(allow_refresh),
                    limit=int(fetch_limit),
                )
            except Exception:
                lots = []

            normalized_lots: List[Tuple[float, float, str]] = []
            for price, lot_float, wear in list(lots or []):
                if lot_float is None:
                    continue
                normalized_lots.append((float(lot_float), float(price), str(wear or '')))

            normalized_lots.sort(key=lambda x: (float(x[0]), float(x[1])))
            depth = len(normalized_lots)
            if min_depth is None or depth < int(min_depth):
                min_depth = int(depth)

            matched = 0
            lot_idx = 0
            for cap in caps_sorted:
                found = False
                while lot_idx < len(normalized_lots):
                    lot_float_f, _lot_price_f, _lot_wear = normalized_lots[lot_idx]
                    lot_idx += 1
                    if float(lot_float_f) <= float(cap) + 1e-9:
                        matched += 1
                        found = True
                        break
                if not found:
                    limiting_skin = str(skin_name)
                    limiting_required = int(len(caps_sorted))
                    limiting_matched = int(matched)
                    result = {
                        'craftable': False,
                        'reason': 'insufficient_matching_listings',
                        'min_depth': int(min_depth or 0),
                        'limiting_skin': limiting_skin,
                        'required_slots': int(limiting_required),
                        'matched_slots': int(limiting_matched),
                    }
                    with self._lock_contract_eval:
                        self._memo_contract_craftability[key] = dict(result)
                    return dict(result)

        result = {
            'craftable': True,
            'reason': 'ok',
            'min_depth': int(min_depth or 0),
            'limiting_skin': None,
            'required_slots': int(len(contract_skins)),
            'matched_slots': int(len(contract_skins)),
        }
        with self._lock_contract_eval:
            self._memo_contract_craftability[key] = dict(result)
        return dict(result)

    def _collection_imbalance_ratio(self, collection_name: str, input_rarity: str, *, is_stattrak: bool) -> Optional[float]:
        input_rarity = self._normalize_rarity(input_rarity)
        memo_key = (collection_name, input_rarity, bool(is_stattrak))
        with self._lock_contract_eval:
            cached = self._memo_collection_imbalance.get(memo_key)
        if cached is not None:
            return float(cached)

        best_wear = self._best_target_wear(collection_name, input_rarity, is_stattrak=is_stattrak)
        outs = self._get_possible_outputs(collection_name, input_rarity, average_float=None, is_stattrak=is_stattrak)
        prices: List[float] = []
        for o in outs:
            p = self._cached_get_price(
                o['name'],
                target_wear=best_wear,
                exclude_stattrak=not is_stattrak,
                require_stattrak=bool(is_stattrak),
                strict_name_match=False,
                allow_refresh=False,
            )
            if p and float(p) > 0:
                prices.append(float(p))

        ratio = None
        if len(prices) >= 1:
            try:
                med = float(statistics.median(prices))
            except Exception:
                med = 0.0
            mx = float(max(prices)) if prices else 0.0
            if med > 1e-12 and mx > 0:
                ratio = mx / med

        with self._lock_contract_eval:
            self._memo_collection_imbalance[memo_key] = float(ratio) if ratio is not None else None
        return float(ratio) if ratio is not None else None

    def _rank_output_targets(
        self,
        *,
        input_rarity: str,
        is_stattrak: bool,
        min_imbalance_ratio: float,
        max_targets: int,
    ) -> List[Dict]:
        input_rarity = self._normalize_rarity(input_rarity)
        memo_key = (input_rarity, bool(is_stattrak), float(min_imbalance_ratio), int(max_targets))
        with self._lock_contract_eval:
            cached = self._memo_target_rank.get(memo_key)
        if cached is not None:
            return list(cached)

        try:
            all_collections = list(self.database.list_collections())
        except Exception:
            all_collections = []

        targets: List[Dict] = []
        dbg = str(os.getenv('HUNT_DEBUG', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
        collections_checked = 0
        collections_passed_imb = 0
        for c in all_collections:
            collections_checked += 1
            imb = self._collection_imbalance_ratio(c, input_rarity, is_stattrak=is_stattrak)
            if dbg and str(c).strip().lower() == 'the revolution collection':
                try:
                    self._logger.info(
                        'HuntDebug Revolution: rarity=%s ST=%s imb=%s min_imb=%s',
                        str(input_rarity),
                        'Y' if bool(is_stattrak) else 'N',
                        str(imb),
                        str(min_imbalance_ratio),
                    )
                except Exception:
                    pass
            if imb is not None and float(imb) + 1e-12 < float(min_imbalance_ratio):
                continue
            collections_passed_imb += 1

            best_wear = self._best_target_wear(c, input_rarity, is_stattrak=is_stattrak)
            outs = self._get_possible_outputs(c, input_rarity, average_float=None, is_stattrak=is_stattrak)
            outcomes_count = self._get_next_grade_skins_count(c, input_rarity, is_stattrak=is_stattrak)
            outcomes_count = max(0, int(outcomes_count))
            if outcomes_count <= 0:
                continue
            base_prob = 1.0 / float(outcomes_count)

            # Estimate the total cost of 10 cheapest input skins.
            # Used to approximate ROI in the ranking score so we prefer collections
            # where the output price is high *relative to the entry cost*, not just in
            # absolute terms.  _get_main_skins is memoized so this is essentially free.
            try:
                # Use max_float consistent with best_wear so the score reflects actual entry cost
                _max_f_for_score = self._wear_to_max_float(best_wear)
                _cheap_ins = self._get_main_skins(
                    c, count=1, is_stattrak=bool(is_stattrak), rarity=input_rarity,
                    max_float=float(_max_f_for_score),
                )
                _input_price_est_10x = float(_cheap_ins[0].get('price') or 0.0) * 10.0 if _cheap_ins else 0.0
            except Exception:
                _input_price_est_10x = 0.0

            if dbg and str(c).strip().lower() == 'the revolution collection':
                try:
                    prices_dbg = []
                    for o in outs:
                        p_dbg = self._cached_get_price(
                            o['name'],
                            target_wear=best_wear,
                            exclude_stattrak=not is_stattrak,
                            require_stattrak=bool(is_stattrak),
                            strict_name_match=False,
                            allow_refresh=False,
                        )
                        if p_dbg and float(p_dbg) > 0:
                            prices_dbg.append(float(p_dbg))
                    self._logger.info(
                        'HuntDebug Revolution: best_wear=%s outs=%d priced_outs=%d outcomes_count=%d prices=%s',
                        str(best_wear),
                        int(len(outs or [])),
                        int(len(prices_dbg)),
                        int(outcomes_count),
                        str(sorted(prices_dbg, reverse=True)[:5]),
                    )
                except Exception:
                    pass

            for o in outs:
                p = self._cached_get_price(
                    o['name'],
                    target_wear=best_wear,
                    exclude_stattrak=not is_stattrak,
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=False,
                    allow_refresh=False,
                )
                if not p or float(p) <= 0:
                    continue
                # Score = probability-weighted EV/cost ratio × imbalance skew.
                # Including imbalance_ratio rewards collections with a "jackpot" skin.
                # Including input cost estimate rewards affordable entry points.
                if _input_price_est_10x > 0:
                    target_score = (float(p) / float(_input_price_est_10x)) * float(base_prob) * float(imb)
                else:
                    target_score = float(p) * float(base_prob) * float(imb)
                targets.append({
                    'target_skin': o['name'],
                    'target_collection': c,
                    'input_rarity': input_rarity,
                    'target_wear': best_wear,
                    'target_price': float(p),
                    'base_probability': float(base_prob),
                    'target_score': float(target_score),
                    'imbalance_ratio': float(imb),
                    'outcomes_count': int(outcomes_count),
                })

        targets.sort(key=lambda x: float(x.get('target_score') or 0.0), reverse=True)
        targets = targets[: int(max_targets)]
        if dbg:
            try:
                logging.getLogger().info(
                    'RankTargets rarity=%s mode=%s: collections=%s passed_imb=%s min_imb=%s targets=%s (max_targets=%s) ST=%s',
                    str(input_rarity),
                    'N/A',
                    int(collections_checked),
                    int(collections_passed_imb),
                    str(min_imbalance_ratio),
                    int(len(targets)),
                    int(max_targets),
                    'Y' if bool(is_stattrak) else 'N',
                )
            except Exception:
                pass
        with self._lock_contract_eval:
            self._memo_target_rank[memo_key] = list(targets)
        return list(targets)

    def _chance_of_target_for_contract(self, contract_skins: List[Dict], *, target_skin: str, is_stattrak: bool) -> float:
        key = (
            tuple(
                sorted(
                    (
                        s.get('name'),
                        s.get('collection'),
                        round(float(s.get('float') or 0.0), 4),
                        round(float(s.get('price') or 0.0), 4),
                    )
                    for s in contract_skins
                )
            ),
            str(target_skin),
            bool(is_stattrak),
        )
        with self._lock_contract_eval:
            cached = self._memo_contract_target_prob.get(key)
        if cached is not None:
            return float(cached)

        prob = 0.0
        try:
            outcomes = self.calculate_contract_outcomes_details(contract_skins, is_stattrak=is_stattrak)
        except Exception:
            outcomes = []
        for o in outcomes:
            if o.get('name') == target_skin:
                prob = float(o.get('probability') or 0.0)
                break

        with self._lock_contract_eval:
            self._memo_contract_target_prob[key] = float(prob)
        return float(prob)

    def _max_output_price_for_contract(self, contract_skins: List[Dict], *, is_stattrak: bool) -> float:
        key = (
            tuple(
                sorted(
                    (
                        s.get('name'),
                        s.get('collection'),
                        round(float(s.get('float') or 0.0), 4),
                        round(float(s.get('price') or 0.0), 4),
                    )
                    for s in contract_skins
                )
            ),
            bool(is_stattrak),
        )
        with self._lock_contract_eval:
            cached = self._memo_contract_max_output.get(key)
        if cached is not None:
            return float(cached)

        mx = 0.0
        try:
            outcomes = self.calculate_contract_outcomes_details(contract_skins, is_stattrak=is_stattrak)
        except Exception:
            outcomes = []
        for o in outcomes:
            p = float(o.get('price') or 0.0)
            if p > mx:
                mx = p

        with self._lock_contract_eval:
            self._memo_contract_max_output[key] = float(mx)
        return float(mx)

    def find_target_hunting_pro_mode(
        self,
        *,
        max_results: int = 20,
        max_investment: Optional[float] = None,
        is_stattrak: bool = False,
        input_rarities: Optional[List[str]] = None,
        min_roi_pct: float = 5.0,
        min_profit_probability: float = 0.40,
        min_imbalance_ratio: float = 1.0,
        min_cost: float = 0.0,
        min_net_profit: float = 0.0,
        exploration_rate: float = 0.10,
        max_targets_per_rarity: int = 120,
        mode: str = 'BALANCED',
        rank_strategy: str = 'DEFAULT',
        jackpot_ratio_threshold: float = 5.0,
    ) -> List[Dict]:
        if input_rarities is None:
            input_rarities = ['Industrial', 'Mil-Spec', 'Restricted', 'Classified']

        results: List[Dict] = []
        splits = [(10, 0), (9, 1), (8, 2), (7, 3), (6, 4), (5, 5)]

        dbg = str(os.getenv('HUNT_DEBUG', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
        dbg_summary = []

        dbg_eval = bool(dbg) and (str(os.getenv('HUNT_DEBUG_EVAL', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'})
        try:
            dbg_eval_max = int(os.getenv('HUNT_DEBUG_EVAL_MAX', '30') or 30)
        except Exception:
            dbg_eval_max = 30
        dbg_eval_n = 0

        dbg_eval_coll = str(os.getenv('HUNT_DEBUG_EVAL_COLL', '') or '').strip().lower()
        dbg_eval_name = str(os.getenv('HUNT_DEBUG_EVAL_NAME', '') or '').strip().lower()
        dbg_eval_best = str(os.getenv('HUNT_DEBUG_EVAL_BEST', '') or '').strip().lower()
        dbg_eval_virt_only = str(os.getenv('HUNT_DEBUG_EVAL_VIRT_ONLY', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
        try:
            _near = str(os.getenv('HUNT_DEBUG_EVAL_NEAR_DELTA', '') or '').strip()
            dbg_eval_near_delta = float(_near) if _near else None
        except Exception:
            dbg_eval_near_delta = None
        dbg_eval_has_filters = bool(dbg_eval_coll or dbg_eval_name or dbg_eval_best or dbg_eval_virt_only or (dbg_eval_near_delta is not None))

        prof = str(os.getenv('HUNT_PROFILE', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
        prof_total_start = time.perf_counter() if prof else 0.0
        prof_totals = {
            'rank_targets_s': 0.0,
            'get_inputs_s': 0.0,
            'get_fillers_s': 0.0,
            'eval_contract_s': 0.0,
            'calculate_outcomes_s': 0.0,
            'targets_considered': 0,
            'contracts_built': 0,
            'eval_calls': 0,
            'outcomes_calls': 0,
        }

        try:
            target_items_limit = int(os.getenv('HUNT_TARGET_ITEMS_LIMIT', '60') or 60)
        except Exception:
            target_items_limit = 60

        try:
            filler_collections_limit = int(os.getenv('HUNT_FILLER_COLLECTIONS_LIMIT', '20') or 20)
        except Exception:
            filler_collections_limit = 20

        mode = str(mode or 'BALANCED').strip().upper()
        if mode not in {'SAFE', 'BALANCED', 'HIGH-RISK', 'HIGH_RISK', 'PROFIT'}:
            mode = 'BALANCED'
        if mode == 'HIGH_RISK':
            mode = 'HIGH-RISK'

        rank_strategy = str(rank_strategy or 'DEFAULT').strip().upper()
        if rank_strategy in {'EV/COST', 'EV_PER_$', 'EV_PER_DOLLAR'}:
            rank_strategy = 'EV_PER_COST'
        if rank_strategy not in {'DEFAULT', 'EV_PER_COST', 'PROFIT'}:
            rank_strategy = 'DEFAULT'

        # Allow env-var override for jackpot threshold regardless of what caller passed.
        # jackpot_ratio_threshold is kept for backward-compat but no longer used for is_jackpot_f;
        # jackpot_min_ev_ratio (HUNT_JACKPOT_MIN_EV_RATIO) is now the single jackpot gate.
        jackpot_ratio_threshold = float(jackpot_ratio_threshold)

        # Jackpot-specific filter parameters (configurable via env).
        try:
            jackpot_min_pp = float(os.getenv('HUNT_JACKPOT_MIN_PP', '0.15') or 0.15)
        except Exception:
            jackpot_min_pp = 0.15

        # Jackpot EV ratio: probability-weighted return of the best outcome alone must
        # cover at least this fraction of input cost.
        # jackpot_ratio * chance_target >= jackpot_min_ev_ratio
        # e.g. 0.5 means the "lottery ticket" covers ≥50% of cost in expectation.
        try:
            jackpot_min_ev_ratio = float(os.getenv('HUNT_JACKPOT_MIN_EV_RATIO', '0.5') or 0.5)
        except Exception:
            jackpot_min_ev_ratio = 0.5

        try:
            require_craftable = str(os.getenv('HUNT_REQUIRE_CRAFTABLE', '1') or '').strip().lower() not in {'0', 'false', 'no', 'off'}
        except Exception:
            require_craftable = True

        broad_csfloat_prev_enabled = None
        try:
            cfc = getattr(self.price_manager, 'csfloat_client', None)
            if cfc is not None:
                broad_csfloat_prev_enabled = bool(getattr(cfc, 'enabled', False))
                setattr(cfc, 'enabled', False)
        except Exception:
            broad_csfloat_prev_enabled = None

        for input_rarity in input_rarities:
            rarity_stats = {
                'rarity': self._normalize_rarity(input_rarity),
                'ranked_targets': 0,
                'targets_considered': 0,
                'targets_with_inputs': 0,
                'contracts_built': 0,
                'contracts_craftable': 0,
                'contracts_ev_ok': 0,
                'contracts_pp_ok': 0,
                'contracts_roi_ok': 0,
                'contracts_jackpot': 0,
                'contracts_jackpot_ok': 0,
                'contracts_selected': 0,
            }
            min_imb = float(min_imbalance_ratio)
            try:
                if self._normalize_rarity(input_rarity) == 'Industrial':
                    min_imb = float(os.getenv('INDUSTRIAL_MIN_IMBALANCE_RATIO', '1.0') or 1.0)
            except Exception:
                min_imb = float(min_imbalance_ratio)

            _t0 = time.perf_counter() if prof else 0.0
            ranked_targets = self._rank_output_targets(
                input_rarity=input_rarity,
                is_stattrak=is_stattrak,
                min_imbalance_ratio=float(min_imb),
                max_targets=int(max_targets_per_rarity),
            )
            if prof:
                prof_totals['rank_targets_s'] += float(time.perf_counter() - _t0)
            rarity_stats['ranked_targets'] = int(len(ranked_targets or []))
            if not ranked_targets:
                if dbg:
                    dbg_summary.append(rarity_stats)
                continue

            # SAFE mode: augment targets with ALL wears per collection.
            # The default best_wear maximises average ROI, but PP=100% may exist at a
            # different wear (e.g. FT inputs are cheap enough that all FT outputs exceed cost).
            if mode == 'SAFE' and ranked_targets:
                _all_wears = ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']
                _seen_cw = set()
                _augmented = []
                for _t in ranked_targets:
                    _ckey = (str(_t.get('target_collection') or ''), str(_t.get('target_wear') or ''))
                    _seen_cw.add(_ckey)
                    _augmented.append(_t)
                # For each unique collection, add representative targets at other wears.
                _seen_collections = set()
                for _t in list(ranked_targets):
                    _tc2 = str(_t.get('target_collection') or '')
                    if _tc2 in _seen_collections:
                        continue
                    _seen_collections.add(_tc2)
                    for _w in _all_wears:
                        if (_tc2, _w) in _seen_cw:
                            continue
                        _seen_cw.add((_tc2, _w))
                        _dup_t = dict(_t)
                        _dup_t['target_wear'] = _w
                        _augmented.append(_dup_t)
                ranked_targets = _augmented

            # Precompute filler collections once per rarity+mode.
            # This was previously recomputed for every target and is extremely expensive.
            try:
                all_collections = list(self.database.list_collections())
            except Exception:
                all_collections = []

            filler_candidates = []
            for c in all_collections:
                outcomes_count = self._get_next_grade_skins_count(c, input_rarity, is_stattrak=is_stattrak)
                if outcomes_count <= 0:
                    continue
                cheap = self._get_main_skins(
                    c,
                    count=1,
                    is_stattrak=is_stattrak,
                    rarity=input_rarity,
                    max_float=1.0,
                )
                if cheap:
                    cheap_price = float(cheap[0].get('price') or 1e9)
                    # Normalized float: lower = closer to FN, preferred for tight float control.
                    cheap_float = float(cheap[0].get('float') or 0.5)
                    skin_nm_f = str(cheap[0].get('name') or '')
                    try:
                        sd_f = self.database.get_skin_by_name(skin_nm_f) if skin_nm_f else None
                        if sd_f:
                            _span_f = max(float(sd_f.max_float) - float(sd_f.min_float), 1e-9)
                            cheap_norm_float = max(0.0, min(1.0, (cheap_float - float(sd_f.min_float)) / _span_f))
                        else:
                            cheap_norm_float = cheap_float
                    except Exception:
                        cheap_norm_float = cheap_float
                else:
                    cheap_price = 1e9
                    cheap_norm_float = 0.5
                filler_candidates.append((int(outcomes_count), cheap_norm_float, cheap_price, c))
            filler_candidates.sort(key=lambda x: (x[0], x[1], x[2]))
            base_filler_collections = [x[3] for x in filler_candidates[: int(max(1, filler_collections_limit))]]

            explore_n = int(max(1, round(float(exploration_rate) * len(ranked_targets)))) if ranked_targets else 0
            explore_set = set()
            if explore_n > 0:
                for t in random.sample(ranked_targets, k=min(explore_n, len(ranked_targets))):
                    explore_set.add(str(t.get('target_skin')))

            for idx, t in enumerate(ranked_targets, start=1):
                rarity_stats['targets_considered'] = int(rarity_stats.get('targets_considered') or 0) + 1
                # Exploration layer: every 20 iterations force a random target from the pool
                if (idx % 20) == 0 and ranked_targets:
                    t = random.choice(ranked_targets)

                target_skin = str(t.get('target_skin'))
                target_c = str(t.get('target_collection'))
                imbalance_ratio = float(t.get('imbalance_ratio') or 1.0)
                best_wear = str(t.get('target_wear') or 'Minimal Wear')
                outcomes_count_for_target = int(t.get('outcomes_count') or 0)

                max_float = self._wear_to_max_float(best_wear)
                # По умолчанию ограничиваем входной float так, чтобы выходной wear соответствовал
                # best_wear (самому доходному). Можно переопределить через HUNT_INPUT_MAX_FLOAT.
                effective_max_float_default = str(os.getenv('HUNT_INPUT_MAX_FLOAT', '') or '').strip()
                try:
                    effective_max_float = float(effective_max_float_default) if effective_max_float_default else float(max_float)
                except Exception:
                    effective_max_float = float(max_float)
                try:
                    if self._normalize_rarity(input_rarity) == 'Industrial':
                        ind_override = str(os.getenv('INDUSTRIAL_INPUT_MAX_FLOAT', '') or '').strip()
                        if ind_override:
                            effective_max_float = float(ind_override)
                except Exception:
                    pass

                _t1 = time.perf_counter() if prof else 0.0
                target_items = self._get_candidate_inputs(
                    target_c,
                    input_rarity,
                    is_stattrak=is_stattrak,
                    max_float=effective_max_float,
                    limit=int(max(10, target_items_limit)),
                )
                if prof:
                    prof_totals['get_inputs_s'] += float(time.perf_counter() - _t1)
                if not target_items:
                    continue
                rarity_stats['targets_with_inputs'] = int(rarity_stats.get('targets_with_inputs') or 0) + 1

                # Choose filler collections: prefer few outcomes and cheap entry.
                # Use the precomputed list and only filter out the target collection.
                filler_collections = [c for c in base_filler_collections if c != target_c]

                # Exploration: sometimes allow mixed fillers by adding a random filler collection candidate
                if target_skin in explore_set and filler_candidates:
                    filler_collections = list(dict.fromkeys(filler_collections + [random.choice(filler_candidates)[3]]))

                best_item = None
                best_score = None

                for target_cnt, filler_cnt in splits:
                    if len(target_items) < int(target_cnt) and len(target_items) > 0:
                        pass

                    target_cnt_i = int(target_cnt)
                    if target_cnt_i <= 0:
                        continue

                    base_targets: List[List[Dict]] = []
                    base_target = list(target_items[: int(max(1, target_cnt_i))])
                    if len(base_target) >= int(target_cnt_i):
                        base_targets.append(base_target)

                    by_name: Dict[str, List[Dict]] = {}
                    for it in list(target_items[: int(max(10, target_items_limit))]):
                        nm = str(it.get('name') or '')
                        if not nm:
                            continue
                        by_name.setdefault(nm, []).append(it)

                    virtual_stack_enabled = str(os.getenv('HUNT_VIRTUAL_STACK', '1') or '').strip().lower() not in {'0', 'false', 'no', 'off'}

                    # prefer_wear controls float penalty in stack sorting.
                    # Use best_wear for this collection so we select items whose float
                    # matches the optimal output wear rather than always pushing toward FN.
                    _prefer_wear_override = str(os.getenv('HUNT_PREFER_OUT_WEAR', '') or '').strip()
                    prefer_wear = _prefer_wear_override if _prefer_wear_override else best_wear
                    wear_thr = {
                        'Factory New': 0.07,
                        'Minimal Wear': 0.15,
        'Field-Tested': 0.38,
                        'Well-Worn': 0.45,
                        'Battle-Scarred': 1.0,
                    }
                    try:
                        prefer_in_norm_max = float(wear_thr.get(prefer_wear, 0.07))
                    except Exception:
                        prefer_in_norm_max = 0.07
                    try:
                        float_penalty_mult = float(os.getenv('HUNT_FLOAT_PENALTY_MULT', '1.5') or 1.5)
                    except Exception:
                        float_penalty_mult = 1.5

                    def _stack_sort_key(x: Dict) -> float:
                        try:
                            p = float(x.get('price') or 0.0)
                        except Exception:
                            p = 0.0
                        try:
                            f = float(x.get('float') or 1.0)
                        except Exception:
                            f = 1.0
                        if p <= 0:
                            p = 1e9

                        nm2 = str(x.get('name') or '')
                        sd = self.database.get_skin_by_name(nm2) if nm2 else None
                        try:
                            min_f = float(sd.min_float) if sd is not None else 0.0
                            max_f = float(sd.max_float) if sd is not None else 1.0
                        except Exception:
                            min_f = 0.0
                            max_f = 1.0
                        denom = float(max_f) - float(min_f)
                        if denom <= 1e-9:
                            norm = 1.0
                        else:
                            norm = (float(f) - float(min_f)) / float(denom)
                            if norm < 0.0:
                                norm = 0.0
                            if norm > 1.0:
                                norm = 1.0

                        if prefer_in_norm_max <= 1e-9:
                            return float(p)
                        if norm <= prefer_in_norm_max + 1e-9:
                            return float(p)
                        excess = (float(norm) - float(prefer_in_norm_max)) / float(prefer_in_norm_max)
                        return float(p) * (1.0 + float(float_penalty_mult) * float(excess))

                    for nm, items in by_name.items():
                        items_sorted = sorted(items, key=_stack_sort_key)

                        if len(items_sorted) >= int(target_cnt_i):
                            stack = list(items_sorted[: int(target_cnt_i)])
                            if len(stack) >= int(target_cnt_i):
                                base_targets.append(stack)
                            continue

                        if virtual_stack_enabled and int(target_cnt_i) == 10 and len(items_sorted) > 0:
                            base = list(items_sorted[: min(10, len(items_sorted))])

                            stack = []
                            for i, it2 in enumerate(base, start=1):
                                d = dict(it2)
                                ik = str(d.get('instance_key') or '')
                                d['instance_key'] = f"{ik}|virt{i}" if ik else f"{nm}|virt{i}"
                                stack.append(d)

                            while len(stack) < 10:
                                i = len(stack) + 1
                                # Cycle through available lots for a more realistic price spread.
                                # If only one lot exists this is equivalent to repeating cheapest.
                                source = base[(i - 1) % len(base)]
                                d = dict(source)
                                ik = str(d.get('instance_key') or '')
                                d['instance_key'] = f"{ik}|virt{i}" if ik else f"{nm}|virt{i}"
                                stack.append(d)

                            if len(stack) == 10:
                                base_targets.append(stack)

                    if not base_targets:
                        continue

                    filler_choices = [None] if filler_cnt == 0 else list(filler_collections)
                    for filler_c in filler_choices:
                        fillers: List[Dict] = []
                        if filler_cnt > 0:
                            if not filler_c:
                                continue
                            fillers = self._get_fillers_from_collection(
                                filler_c,
                                input_rarity,
                                int(max(200, filler_cnt * 80)),
                                is_stattrak,
                                target_float_threshold=float(effective_max_float) if float(effective_max_float) < 0.999 else None,
                                max_price=None,
                            )
                            if len(fillers) <= 0:
                                continue
                            fillers = list(fillers[: int(filler_cnt)])
                            if len(fillers) < int(filler_cnt):
                                continue

                        for base_target2 in base_targets:
                            contract = list(base_target2) + list(fillers)
                            if len(contract) != 10:
                                continue

                            rarity_stats['contracts_built'] = int(rarity_stats.get('contracts_built') or 0) + 1

                            craftability = {
                                'craftable': True,
                                'min_depth': 0,
                                'reason': 'skipped',
                            }
                            if bool(require_craftable):
                                craftability = self._check_contract_craftability(
                                    contract,
                                    is_stattrak=is_stattrak,
                                    allow_unknown_float=False,
                                    allow_refresh=False,
                                )
                                if not bool(craftability.get('craftable')):
                                    continue
                                rarity_stats['contracts_craftable'] = int(rarity_stats.get('contracts_craftable') or 0) + 1

                            if prof:
                                prof_totals['contracts_built'] = int(prof_totals.get('contracts_built') or 0) + 1
                            _t2 = time.perf_counter() if prof else 0.0
                            ev = self._evaluate_contract_cached(contract, target_c, is_stattrak=is_stattrak)
                            if prof:
                                prof_totals['eval_calls'] = int(prof_totals.get('eval_calls') or 0) + 1
                                prof_totals['eval_contract_s'] += float(time.perf_counter() - _t2)
                            input_cost = float(ev.get('input_cost') or 0.0)
                            expected_output = float(ev.get('expected_output') or 0.0)
                            roi = float(ev.get('roi') or 0.0)
                            pp = float(ev.get('profit_probability') or 0.0)

                            if dbg_eval and dbg_eval_n < int(dbg_eval_max):
                                try:
                                    virt_used = False
                                    for s in contract:
                                        ik = str(s.get('instance_key') or '')
                                        if '|virt' in ik:
                                            virt_used = True
                                            break

                                    base_nm = str(base_target2[0].get('name') if base_target2 else '')
                                    coll_nm = str(target_c)
                                    best_nm = str(ev.get('best_outcome_name') or '')

                                    should_log = False
                                    if not dbg_eval_has_filters:
                                        should_log = (self._normalize_rarity(input_rarity) == 'Restricted') and (not bool(is_stattrak))
                                    else:
                                        should_log = True
                                        if dbg_eval_virt_only and not bool(virt_used):
                                            should_log = False
                                        if should_log and dbg_eval_coll and (dbg_eval_coll not in coll_nm.lower()):
                                            should_log = False
                                        if should_log and dbg_eval_name and (dbg_eval_name not in base_nm.lower()):
                                            should_log = False
                                        if should_log and dbg_eval_best and (dbg_eval_best not in best_nm.lower()):
                                            should_log = False
                                        if should_log and (dbg_eval_near_delta is not None):
                                            if abs(float(expected_output) - float(input_cost)) > float(dbg_eval_near_delta):
                                                should_log = False

                                    if not should_log:
                                        raise RuntimeError('skip')

                                    self._logger.info(
                                        'HuntDebugEval rarity=%s ST=%s coll=%s nm=%s split=%sx%s virt=%s cost=%.3f ev=%.3f roi=%.3f pp=%.3f best=%s best_price=%.3f best_prob=%.3f avg_norm=%.4f',
                                        str(self._normalize_rarity(input_rarity)),
                                        'Y' if bool(is_stattrak) else 'N',
                                        str(coll_nm),
                                        str(base_nm),
                                        int(target_cnt),
                                        int(filler_cnt),
                                        'Y' if bool(virt_used) else 'N',
                                        float(input_cost),
                                        float(expected_output),
                                        float(roi),
                                        float(pp),
                                        str(best_nm),
                                        float(ev.get('best_outcome_price') or 0.0),
                                        float(ev.get('best_outcome_probability') or 0.0),
                                        float(ev.get('average_normalized_float') or 0.0),
                                    )

                                    try:
                                        in_floats = [float(s.get('float')) for s in contract if s.get('float') is not None]
                                        in_min = min(in_floats) if in_floats else 0.0
                                        in_max = max(in_floats) if in_floats else 0.0
                                        in_avg = (sum(in_floats) / float(len(in_floats))) if in_floats else 0.0

                                        outs_dbg = self.calculate_contract_outcomes_details(contract, is_stattrak=is_stattrak)
                                        outs_dbg = list(outs_dbg or [])
                                        outs_dbg.sort(key=lambda x: float(x.get('price') or 0.0), reverse=True)
                                        top = []
                                        for o in outs_dbg[:8]:
                                            top.append(
                                                f"{str(o.get('name') or '')}"
                                                f"|{str(o.get('wear') or '')}"
                                                f"|{float(o.get('out_float') or 0.0):.4f}"
                                                f"|{float(o.get('price') or 0.0):.3f}"
                                                f"|p={float(o.get('probability') or 0.0):.3f}"
                                            )

                                        self._logger.info(
                                            'HuntDebugEvalDetails in_float[min=%.5f max=%.5f avg=%.5f] outcomes_top=%s',
                                            float(in_min),
                                            float(in_max),
                                            float(in_avg),
                                            str(top),
                                        )
                                    except Exception:
                                        pass
                                    dbg_eval_n += 1
                                except Exception:
                                    pass

                            best_out_name = str(ev.get('best_outcome_name') or '')
                            best_out_price = float(ev.get('best_outcome_price') or 0.0)
                            best_out_prob = float(ev.get('best_outcome_probability') or 0.0)
                            if (not best_out_name) or best_out_price <= 0.0:
                                continue

                            best_out = {
                                'name': best_out_name,
                                'price': best_out_price,
                                'probability': best_out_prob,
                            }

                            chance_target = float(best_out_prob)
                            if chance_target <= 0.0:
                                continue

                            max_output_price = float(best_out_price)
                            jackpot_ratio = (float(max_output_price) / float(input_cost)) if float(input_cost) > 0 else 0.0

                            def _opportunity_score(evv: Dict, ct: float, jr: float) -> float:
                                ic = float(evv.get('input_cost') or 0.0)
                                eo = float(evv.get('expected_output') or 0.0)
                                profit = eo - ic
                                cpp = float(evv.get('profit_probability') or 0.0)
                                if ic <= 0 or cpp <= 0 or ct <= 0:
                                    return -1e18
                                if mode == 'SAFE':
                                    return float(cpp) * float(ct) * float(imbalance_ratio)
                                if mode == 'HIGH-RISK':
                                    return float(jr) * float(ct) * float(imbalance_ratio)
                                return float(profit) * float(cpp) * float(ct) * float(imbalance_ratio)

                            current = list(contract)
                            current_eval = dict(ev)
                            current_best_out = dict(best_out)
                            current_chance_target = float(chance_target)
                            current_jackpot_ratio = float(jackpot_ratio)

                            best_local_score = _opportunity_score(current_eval, current_chance_target, current_jackpot_ratio)

                            # Final scoring
                            input_cost_f = float(current_eval.get('input_cost') or 0.0)
                            expected_output_f = float(current_eval.get('expected_output') or 0.0)
                            profit_f = expected_output_f - input_cost_f
                            pp_f = float(current_eval.get('profit_probability') or 0.0)
                            roi_f = float(current_eval.get('roi') or 0.0)
                            jackpot_ratio_f = float(current_jackpot_ratio)
                            # Jackpot is declared when the probability-weighted return of the best
                            # outcome alone covers at least jackpot_min_ev_ratio of input cost.
                            # This is continuous and penalises low-probability "jackpots" (e.g.
                            # ratio=6 but prob=0.04 → ev_ratio=0.24 < 0.5 → NOT jackpot).
                            _jackpot_ev_ratio_f = float(jackpot_ratio_f) * float(current_chance_target)
                            is_jackpot_f = bool(_jackpot_ev_ratio_f >= float(jackpot_min_ev_ratio))

                            if float(min_cost) > 0.0 and input_cost_f + 1e-9 < float(min_cost):
                                continue
                            if (not is_jackpot_f) and float(min_net_profit) > 0.0 and profit_f + 1e-9 < float(min_net_profit):
                                continue

                            # Late filtering (mode-aware). Jackpot contracts can bypass ROI/EV thresholds
                            # but still must pass dedicated jackpot-specific criteria.
                            if not is_jackpot_f:
                                if expected_output_f + 1e-9 < input_cost_f:
                                    continue
                                rarity_stats['contracts_ev_ok'] = int(rarity_stats.get('contracts_ev_ok') or 0) + 1
                                if roi_f + 1e-9 < float(min_roi_pct):
                                    continue
                                rarity_stats['contracts_roi_ok'] = int(rarity_stats.get('contracts_roi_ok') or 0) + 1
                                if pp_f + 1e-12 < float(min_profit_probability):
                                    continue
                                rarity_stats['contracts_pp_ok'] = int(rarity_stats.get('contracts_pp_ok') or 0) + 1
                            else:
                                rarity_stats['contracts_jackpot'] = int(rarity_stats.get('contracts_jackpot') or 0) + 1
                                # Jackpot filter: minimum profit probability and ROI (same thresholds as regular contracts).
                                if pp_f + 1e-12 < max(float(jackpot_min_pp), float(min_profit_probability)):
                                    continue
                                if roi_f + 1e-9 < float(min_roi_pct):
                                    continue
                                rarity_stats['contracts_jackpot_ok'] = int(rarity_stats.get('contracts_jackpot_ok') or 0) + 1

                            item = dict(current_eval)
                            item.update({
                                'target_collection': target_c,
                                'is_stattrak': bool(is_stattrak),
                                'input_skins': current,
                                'main_skins_count': int(target_cnt),
                                'filler_skins_count': int(filler_cnt),
                                'hunt_output': str(current_best_out.get('name') or ''),
                                'hunt_output_price': float(current_best_out.get('price') or 0.0),
                                'hunt_target_wear': best_wear,
                                'hunt_expected_wear': self._determine_wear_from_float(float(current_eval.get('average_normalized_float') or 0.0)),
                                'hunt_input_rarity': self._normalize_rarity(input_rarity),
                                'hunt_filler_collection': filler_c,
                                'chance_of_target': float(current_chance_target),
                                'imbalance_ratio': float(imbalance_ratio),
                                'jackpot_ratio': float(jackpot_ratio_f),
                                'is_jackpot': bool(is_jackpot_f),
                                'outcomes_count': int(outcomes_count_for_target),
                                'craftable': True,
                                'craftability_min_depth': int(craftability.get('min_depth') or 0),
                            })

                            if input_cost_f > 0.0:
                                item['ev_per_cost'] = float(expected_output_f) / float(input_cost_f)
                            else:
                                item['ev_per_cost'] = 0.0

                            # Temporarily keep a mode-specific score for within-target selection
                            if mode == 'SAFE':
                                local_score = float(pp_f) * float(current_chance_target) * float(imbalance_ratio)
                            elif mode == 'HIGH-RISK':
                                local_score = float(jackpot_ratio_f) * float(current_chance_target) * float(imbalance_ratio)
                            elif mode == 'PROFIT':
                                local_score = float(profit_f)
                            else:
                                local_score = float(profit_f) * float(pp_f) * float(current_chance_target) * float(imbalance_ratio)

                            item['opportunity_score'] = float(local_score)

                            if rank_strategy == 'EV_PER_COST':
                                local_rank_score = float(item.get('ev_per_cost') or 0.0)
                            elif rank_strategy == 'PROFIT':
                                local_rank_score = float(profit_f)
                            else:
                                local_rank_score = float(item.get('opportunity_score') or 0.0)

                            item['_local_rank_score'] = float(local_rank_score)

                            if best_item is None or float(item.get('_local_rank_score') or 0.0) > float(best_score or 0.0):
                                best_item = item
                                best_score = float(item.get('_local_rank_score') or 0.0)

                if best_item is not None:
                    results.append(best_item)
                    rarity_stats['contracts_selected'] = int(rarity_stats.get('contracts_selected') or 0) + 1

            if dbg:
                dbg_summary.append(rarity_stats)

        if prof:
            try:
                dur = float(time.perf_counter() - prof_total_start)
                logging.getLogger().info(
                    'HuntProfile mode=%s ST=%s dur_s=%.1f rank_targets_s=%.1f get_inputs_s=%.1f get_fillers_s=%.1f eval_contract_s=%.1f eval_calls=%s contracts_built=%s',
                    str(mode),
                    'Y' if bool(is_stattrak) else 'N',
                    float(dur),
                    float(prof_totals.get('rank_targets_s') or 0.0),
                    float(prof_totals.get('get_inputs_s') or 0.0),
                    float(prof_totals.get('get_fillers_s') or 0.0),
                    float(prof_totals.get('eval_contract_s') or 0.0),
                    int(prof_totals.get('eval_calls') or 0),
                    int(prof_totals.get('contracts_built') or 0),
                )
            except Exception:
                pass

        if dbg:
            try:
                logging.getLogger().info(
                    'HuntDebug mode=%s rarity_summary=%s ST=%s thresholds: roi>=%s pp>=%s imb>=%s max_results=%s max_targets_per_rarity=%s explore=%s',
                    str(mode),
                    str(dbg_summary),
                    'Y' if bool(is_stattrak) else 'N',
                    str(min_roi_pct),
                    str(min_profit_probability),
                    str(min_imbalance_ratio),
                    str(max_results),
                    str(max_targets_per_rarity),
                    str(exploration_rate),
                )
            except Exception:
                pass

        # de-duplicate by target skin (most expensive outcome). Keep the best opportunity per skin.
        # For SAFE mode, include wear in the key so PP=100% at different wears both survive.
        uniq: Dict[Tuple, Dict] = {}
        for r in results:
            if mode == 'SAFE':
                sig = (str(r.get('hunt_output') or ''), bool(r.get('is_stattrak')), str(r.get('hunt_target_wear') or ''))
            else:
                sig = (str(r.get('hunt_output') or ''), bool(r.get('is_stattrak')))
            prev = uniq.get(sig)
            if prev is None:
                uniq[sig] = r
                continue

            try:
                s_new = float(r.get('opportunity_score') or 0.0)
            except Exception:
                s_new = 0.0
            try:
                s_old = float(prev.get('opportunity_score') or 0.0)
            except Exception:
                s_old = 0.0

            if s_new > s_old + 1e-12:
                uniq[sig] = r
                continue

            if abs(s_new - s_old) <= 1e-12:
                try:
                    p_new = float(r.get('expected_output') or 0.0) - float(r.get('input_cost') or 0.0)
                except Exception:
                    p_new = 0.0
                try:
                    p_old = float(prev.get('expected_output') or 0.0) - float(prev.get('input_cost') or 0.0)
                except Exception:
                    p_old = 0.0
                if p_new > p_old + 1e-12:
                    uniq[sig] = r
        results = list(uniq.values())

        # Multi-score ranking (end-stage)
        profits = [float(r.get('expected_output') or 0.0) - float(r.get('input_cost') or 0.0) for r in results]
        min_p = min(profits) if profits else 0.0
        max_p = max(profits) if profits else 0.0
        span_p = (max_p - min_p) if (max_p - min_p) > 1e-12 else 1.0

        roi_vals = [float(r.get('roi') or 0.0) for r in results]
        min_roi = min(roi_vals) if roi_vals else 0.0
        max_roi = max(roi_vals) if roi_vals else 0.0
        span_roi = (max_roi - min_roi) if (max_roi - min_roi) > 1e-12 else 1.0

        jr_vals = [float(r.get('jackpot_ratio') or 0.0) for r in results]
        min_jr = min(jr_vals) if jr_vals else 0.0
        max_jr = max(jr_vals) if jr_vals else 0.0
        span_jr = (max_jr - min_jr) if (max_jr - min_jr) > 1e-12 else 1.0

        for r in results:
            profit = float(r.get('expected_output') or 0.0) - float(r.get('input_cost') or 0.0)
            norm_ev = (profit - float(min_p)) / float(span_p)
            norm_roi = (float(r.get('roi') or 0.0) - float(min_roi)) / float(span_roi)
            norm_jr = (float(r.get('jackpot_ratio') or 0.0) - float(min_jr)) / float(span_jr)
            outcomes_count = float(r.get('outcomes_count') or 0.0)
            liquidity_score = 1.0 / max(1.0, float(outcomes_count))
            r['normalized_ev'] = float(norm_ev)
            r['liquidity_score'] = float(liquidity_score)
            r['final_score'] = (
                0.35 * float(norm_ev)
                + 0.25 * float(norm_roi)
                + 0.20 * float(norm_jr)
                + 0.20 * float(liquidity_score)
            )

            # base rank score (mode-aware)
            if rank_strategy == 'EV_PER_COST':
                r['_base_rank_score'] = float(r.get('ev_per_cost') or 0.0)
            elif rank_strategy == 'PROFIT':
                r['_base_rank_score'] = float(profit)
            else:
                if mode == 'SAFE':
                    r['_base_rank_score'] = (float(r.get('profit_probability') or 0.0) * 1_000_000.0) + float(r.get('final_score') or 0.0)
                elif mode == 'HIGH-RISK':
                    r['_base_rank_score'] = (float(r.get('jackpot_ratio') or 0.0) * 1_000_000.0) + float(r.get('final_score') or 0.0)
                elif mode == 'PROFIT':
                    r['_base_rank_score'] = float(profit)
                else:
                    r['_base_rank_score'] = float(r.get('final_score') or 0.0)

        def _combo_similarity(a: Dict, b: Dict) -> float:
            a_names = [str(s.get('name') or '') for s in (a.get('input_skins') or [])]
            b_names = [str(s.get('name') or '') for s in (b.get('input_skins') or [])]
            if not a_names or not b_names:
                return 0.0
            ca = Counter(a_names)
            cb = Counter(b_names)
            inter = 0
            for k, va in ca.items():
                vb = cb.get(k)
                if vb:
                    inter += int(min(int(va), int(vb)))
            denom = max(1, int(min(len(a_names), len(b_names))))
            return float(inter) / float(denom)

        # Diversity penalty selection (avoid spam of near-identical combos)
        candidates = list(results)
        candidates.sort(key=lambda x: float(x.get('_base_rank_score') or 0.0), reverse=True)
        selected: List[Dict] = []
        while candidates and len(selected) < int(max_results):
            best = None
            best_adj = None
            best_sim = 0.0
            best_mult = 1.0

            for cand in candidates:
                sim = 0.0
                if selected:
                    sim = max(_combo_similarity(cand, s) for s in selected)

                mult = 1.0
                if sim > 0.70:
                    mult = float(0.70) / float(sim)

                base = float(cand.get('_base_rank_score') or 0.0)
                adj = base * float(mult)
                if best is None or adj > float(best_adj or -1e18):
                    best = cand
                    best_adj = float(adj)
                    best_sim = float(sim)
                    best_mult = float(mult)

            if best is None:
                break

            candidates.remove(best)
            best['diversity_similarity'] = float(best_sim)
            best['diversity_multiplier'] = float(best_mult)
            best['_rank_score'] = float(best_adj or 0.0)
            try:
                best['final_score'] = float(best.get('final_score') or 0.0) * float(best_mult)
            except Exception:
                pass
            selected.append(best)

        results = list(selected)

        # final ordering
        if mode == 'SAFE':
            results.sort(key=lambda x: (float(x.get('_rank_score') or x.get('_base_rank_score') or 0.0), float(x.get('final_score') or 0.0)), reverse=True)
        elif mode == 'HIGH-RISK':
            results.sort(key=lambda x: (float(x.get('_rank_score') or x.get('_base_rank_score') or 0.0), float(x.get('final_score') or 0.0)), reverse=True)
        elif mode == 'PROFIT':
            results.sort(key=lambda x: float(x.get('_rank_score') or x.get('_base_rank_score') or 0.0), reverse=True)
        else:
            results.sort(key=lambda x: float(x.get('_rank_score') or x.get('_base_rank_score') or x.get('final_score') or 0.0), reverse=True)

        results = results[: int(max_results)]

        try:
            if broad_csfloat_prev_enabled is not None:
                cfc = getattr(self.price_manager, 'csfloat_client', None)
                if cfc is not None:
                    setattr(cfc, 'enabled', bool(broad_csfloat_prev_enabled))
        except Exception:
            pass

        # Two-pass refine (optional). Broad pass is calculated with CSFloat disabled in order to avoid 429.
        try:
            refine_enable = str(os.getenv('HUNT_REFINE_ENABLE', '1') or '').strip().lower() not in {'0', 'false', 'no', 'off'}
        except Exception:
            refine_enable = True
        try:
            refine_topk = int(os.getenv('HUNT_REFINE_TOPK', '200') or 200)
        except Exception:
            refine_topk = 200
        if refine_topk < 0:
            refine_topk = 0

        if (not refine_enable) or (int(refine_topk) <= 0) or (not results):
            return results

        k = min(int(refine_topk), len(results))
        refine_slice = list(results[:k])

        # Enable CSFloat only for refine — but ONLY if it's actually usable
        # (has an API key and is not in a rate-limit cooldown). Forcefully enabling
        # a rate-limited CSFloat client causes the refine step to block for 60–120 s.
        csfloat_prev_enabled = None
        try:
            cfc = getattr(self.price_manager, 'csfloat_client', None)
            if cfc is not None:
                csfloat_prev_enabled = bool(getattr(cfc, 'enabled', False))
                has_key = bool(str(getattr(cfc, 'api_key', '') or '').strip())
                is_rate_limited = False
                try:
                    rl_until = float(getattr(cfc, '_rate_limit_until_ts', 0.0) or 0.0)
                    is_rate_limited = rl_until > time.time()
                except Exception:
                    pass
                if has_key and not is_rate_limited:
                    setattr(cfc, 'enabled', True)
                # else: leave CSFloat disabled — refine will use market.csgo.com prices only
        except Exception:
            csfloat_prev_enabled = None

        refined: List[Dict] = []
        self._multisource_net_pricing = True

        # Threshold below which we perform a real-market liquidity check.
        # Configurable via env; default 0.035 — skins this clean are rare and expensive.
        try:
            _liq_float_threshold = float(os.getenv('HUNT_LIQUIDITY_FLOAT_THRESHOLD', '0.035') or 0.035)
        except Exception:
            _liq_float_threshold = 0.035
        try:
            _liq_min_depth = int(os.getenv('HUNT_LIQUIDITY_MIN_DEPTH', '30') or 30)
        except Exception:
            _liq_min_depth = 30
        # How many listings to request per skin (must be > min_depth to detect shortfall).
        _liq_fetch_limit = max(int(_liq_min_depth) + 20, 60)

        try:
            try:
                self.clear_price_memoization()
            except Exception:
                pass

            for r in refine_slice:
                try:
                    contract_skins = list(r.get('input_skins') or [])
                    target_collection = str(r.get('target_collection') or '')
                    is_st = bool(r.get('is_stattrak'))
                    if not contract_skins or not target_collection:
                        refined.append(r)
                        continue

                    # ── Liquidity check ──────────────────────────────────────────────
                    # For skins whose required float ≤ threshold we verify real market
                    # depth and update input prices to reflect slippage (you need to buy
                    # N copies, not just the single cheapest).
                    liquidity_depth: Optional[int] = None
                    if bool(require_craftable):
                        craftability = self._check_contract_craftability(
                            contract_skins,
                            is_stattrak=is_st,
                            allow_unknown_float=False,
                            allow_refresh=False,
                        )
                        if not bool(craftability.get('craftable')):
                            continue
                        liquidity_depth = int(craftability.get('min_depth') or 0)
                    liquidity_ok = True

                    # Aggregate slots needed per skin name and the strictest (minimum)
                    # float cap required across all lots of that skin.
                    _skin_slots: Dict[str, int] = {}       # name -> count needed
                    _skin_max_float: Dict[str, float] = {} # name -> minimum(floats) = strictest cap
                    for _s in contract_skins:
                        _nm = str(_s.get('name') or '')
                        if not _nm:
                            continue
                        _f = float(_s.get('float') or 1.0)
                        _skin_slots[_nm] = _skin_slots.get(_nm, 0) + 1
                        if _nm not in _skin_max_float or _f < _skin_max_float[_nm]:
                            _skin_max_float[_nm] = _f

                    # Build a name→index map to update prices after slippage calculation
                    _skin_real_prices: Dict[str, List[float]] = {} # name -> sorted real prices

                    for _skin_nm, _count_needed in _skin_slots.items():
                        _strictest_float = _skin_max_float.get(_skin_nm, 1.0)
                        if _strictest_float > float(_liq_float_threshold) + 1e-9:
                            # Normal float — no liquidity concern, keep original price
                            continue

                        try:
                            _listings = self.price_manager.get_listings(
                                _skin_nm,
                                max_float=float(_strictest_float),
                                exclude_stattrak=not bool(is_st),
                                require_stattrak=bool(is_st),
                                limit=int(_liq_fetch_limit),
                            )
                        except Exception:
                            _listings = []

                        _depth = len(_listings) if _listings else 0

                        # Track minimum depth across all low-float skins
                        if liquidity_depth is None or _depth < liquidity_depth:
                            liquidity_depth = _depth

                        if _depth < int(_liq_min_depth):
                            liquidity_ok = False
                            break

                        # Collect the real prices for the N lots we need to buy
                        if _listings and _count_needed > 0:
                            _real_prices = [float(_l[0]) for _l in _listings[:_count_needed]]
                            _skin_real_prices[_skin_nm] = _real_prices

                    if not liquidity_ok:
                        continue

                    # Slippage: update input_skins prices with real market prices so that
                    # _calculate_contract_profit reflects the true cost of assembling the contract.
                    if _skin_real_prices:
                        _price_iter: Dict[str, int] = {}  # name -> how many we've assigned
                        updated_skins = []
                        for _s in contract_skins:
                            _nm = str(_s.get('name') or '')
                            if _nm in _skin_real_prices:
                                _idx = _price_iter.get(_nm, 0)
                                _real_p_list = _skin_real_prices[_nm]
                                if _idx < len(_real_p_list):
                                    _s2 = dict(_s)
                                    _s2['price'] = float(_real_p_list[_idx])
                                    updated_skins.append(_s2)
                                    _price_iter[_nm] = _idx + 1
                                else:
                                    updated_skins.append(dict(_s))
                            else:
                                updated_skins.append(dict(_s))
                        contract_skins = updated_skins
                    # ── End liquidity check ──────────────────────────────────────────

                    refined_inputs = self._refine_contract_inputs(contract_skins, is_stattrak=is_st)
                    if refined_inputs:
                        contract_skins = refined_inputs

                    # ── Float optimization (pushing to the edge of quality) ──────────
                    try:
                        optimize_floats = str(os.getenv('HUNT_OPTIMIZE_FLOATS', '1') or '').strip().lower() not in {'0', 'false', 'no', 'off'}
                        if optimize_floats:
                            target_wear = str(r.get('hunt_target_wear') or 'Factory New')
                            optimized_skins = self._optimize_contract_floats(contract_skins, target_wear=target_wear, is_stattrak=is_st)
                            if optimized_skins:
                                contract_skins = optimized_skins
                    except Exception:
                        pass
                    # ── End float optimization ───────────────────────────────────────

                    cd = self._calculate_contract_profit(contract_skins, target_collection, is_st)
                    out = dict(r)
                    out['input_skins'] = contract_skins
                    out.update(cd)
                    if liquidity_depth is not None:
                        out['liquidity_depth'] = int(liquidity_depth)
                    refined.append(out)
                except Exception:
                    refined.append(r)
        finally:
            self._multisource_net_pricing = False

            try:

                self.clear_price_memoization()
            except Exception:
                pass
            try:
                if csfloat_prev_enabled is not None:
                    cfc = getattr(self.price_manager, 'csfloat_client', None)
                    if cfc is not None:
                        setattr(cfc, 'enabled', bool(csfloat_prev_enabled))
            except Exception:
                pass

        merged = list(refined) + list(results[k:])

        # Post-refine filter: re-check thresholds with updated prices.
        # For SAFE mode (PP=100%), skip the PP re-check: the initial eval calculated PP
        # using gross market prices, but the refine step uses net prices
        # (_multisource_net_pricing=True), which deflates outcomes by the selling fee.
        # Re-checking PP with those net-priced values would falsely discard contracts
        # that genuinely have every outcome's market value above input cost.
        # The ROI check still applies (it's consistently fee-aware in both passes).
        _safe_mode = float(min_profit_probability) >= 1.0 - 1e-9
        if float(min_profit_probability) > 0.0 or float(min_roi_pct) > -1e9:
            filtered_merged = []
            for _r in merged:
                _pp = float(_r.get('profit_probability') or 0.0)
                _roi = float(_r.get('roi') or 0.0)
                if (not _safe_mode) and _pp + 1e-12 < float(min_profit_probability):
                    continue
                if _roi + 1e-9 < float(min_roi_pct):
                    continue
                filtered_merged.append(_r)
            merged = filtered_merged

        try:
            merged.sort(key=lambda x: float(x.get('_rank_score') or x.get('contract_score') or x.get('final_score') or 0.0), reverse=True)
        except Exception:
            pass
        return merged[: int(max_results)]

    def _refine_contract_inputs(self, contract_skins: List[Dict], *, is_stattrak: bool) -> Optional[List[Dict]]:
        """Re-select cheaper buy lots across sources but preserve float constraints by capping max_float to the original lot's float."""
        if not contract_skins:
            return None
        out: List[Dict] = []
        changed = False
        for s in contract_skins:
            try:
                nm = str(s.get('name') or '')
                if not nm:
                    out.append(dict(s))
                    continue
                max_f = s.get('float')
                try:
                    max_f2 = float(max_f) if max_f is not None else None
                except Exception:
                    max_f2 = None
                tw = s.get('wear')
                tw2 = str(tw) if tw is not None else None

                best = None
                try:
                    pm = self.price_manager
                    if hasattr(pm, 'get_best_buy_with_float'):
                        best = pm.get_best_buy_with_float(
                            nm,
                            target_wear=tw2,
                            max_float=max_f2,
                            exclude_stattrak=not bool(is_stattrak),
                            require_stattrak=bool(is_stattrak),
                        )
                except Exception:
                    best = None

                if best:
                    price, flt, wear, src = best
                    s2 = dict(s)
                    old_p = float(s2.get('price') or 0.0)
                    s2['price'] = float(price)
                    if flt is not None:
                        s2['float'] = float(flt)
                    if wear:
                        s2['wear'] = str(wear)
                    s2['buy_source'] = str(src)
                    out.append(s2)
                    if old_p <= 0 or float(price) < old_p - 1e-9:
                        changed = True
                else:
                    out.append(dict(s))
            except Exception:
                out.append(dict(s))
        return out if changed else None

    def _optimize_contract_floats(self, contract_skins: List[Dict], *, target_wear: str, is_stattrak: bool) -> Optional[List[Dict]]:
        """
        Optimizes the contract by finding the cheapest skins that keep the output float 
        at the edge of the target quality (e.g., ~0.0699 for Factory New).
        """
        if not contract_skins or len(contract_skins) != 10:
            return None

        wear_thresholds = {
            'Factory New': 0.07,
            'Minimal Wear': 0.15,
            'Field-Tested': 0.38,
            'Well-Worn': 0.45,
            'Battle-Scarred': 1.0,
        }

        
        target_max_avg_norm = float(wear_thresholds.get(target_wear, 0.07))
        # Safety margin: aim for 99.8% of the threshold to avoid rounding issues
        safe_target_avg_norm = target_max_avg_norm * 0.998
        
        current_skins = [dict(s) for s in contract_skins]
        
        # Шаг 6: Используем avg_float (абсолютное среднее) вместо avg_norm
        # out_float = avg_float * (max_f - min_f) + min_f
        # Условие: out_float <= threshold → avg_float <= (threshold - min_f) / (max_f - min_f)
        
        outcomes = self.calculate_contract_outcomes_details(current_skins, is_stattrak=is_stattrak)
        if not outcomes:
            return None

        # Determine the bottleneck: which outcome hits the threshold first?
        # out_float_i = f' * (max_i - min_i) + min_i
        # Condition: out_float_i <= threshold → f' <= (threshold - min_i) / (max_i - min_i)
        
        limit_avg_norm = 1.0
        for o in outcomes:
            min_f = float(o.get('min_float', 0.0))
            max_f = float(o.get('max_float', 1.0))
            denom = max_f - min_f
            if denom > 1e-9:
                max_norm_for_this = (target_max_avg_norm - min_f) / denom
                if max_norm_for_this < limit_avg_norm:
                    limit_avg_norm = max_norm_for_this

        # Apply safety margin to the bottleneck avg_norm
        target_avg_norm = max(0.0, limit_avg_norm * 0.998)
        
        # Store the target max avg norm in the contract dict for UI
        for s in current_skins:
            s['target_max_avg_float'] = round(target_avg_norm, 6)
        
        # We want avg of per-skin normalized floats <= target_avg_norm
        # Each skin's norm = (float - min) / (max - min)
        # sum(norm_i) / 10 <= target_avg_norm → sum(norm_i) <= target_avg_norm * 10
        target_total_norm = target_avg_norm * 10.0
        
        # Sort skins by price (descending) or by how much we can "worsen" them?
        # Let's try to worsen the most expensive skins first to get the most profit.
        indexed_skins = list(enumerate(current_skins))
        
        changed = False
        
        # We will iterate and try to replace each skin with a cheaper one that has a higher float,
        # but keep the total normalized float below target_total_norm.
        
        for i, skin in indexed_skins:
            nm = str(skin.get('name', ''))
            skin_data = self.database.get_skin_by_name(nm)
            if not skin_data:
                continue
            
            try:
                min_f = float(skin_data.min_float)
                max_f = float(skin_data.max_float)
            except Exception:
                continue
                
            if max_f <= min_f + 1e-9:
                continue

            curr_f = float(skin.get('float', 0.0))
            curr_norm = (curr_f - min_f) / (max_f - min_f) if (max_f - min_f) > 1e-9 else 0.0
            curr_norm = max(0.0, min(1.0, curr_norm))

            other_total_norm = 0.0
            for j, s in enumerate(current_skins):
                if i == j:
                    continue
                sf = float(s.get('float', 0.0))
                sn = str(s.get('name') or '')
                sd2 = self.database.get_skin_by_name(sn) if sn else None
                if sd2:
                    smin2 = float(sd2.min_float)
                    smax2 = float(sd2.max_float)
                    sd2_denom = smax2 - smin2
                    if sd2_denom > 1e-9:
                        other_total_norm += max(0.0, min(1.0, (sf - smin2) / sd2_denom))
                    else:
                        other_total_norm += 0.0
                else:
                    other_total_norm += sf

            max_norm_allowed = target_total_norm - other_total_norm
            if max_norm_allowed > 1.0:
                max_norm_allowed = 1.0

            if max_norm_allowed <= curr_norm + 1e-4:
                continue

            # Convert max_norm back to absolute float for this skin
            max_actual_float = max_norm_allowed * (max_f - min_f) + min_f
            
            # Find the best (cheapest) skin on the market with float <= max_actual_float
            # and hopefully float > curr_f
            try:
                pm = self.price_manager
                if hasattr(pm, 'get_best_buy_with_float'):
                    # We look for a skin that is potentially cheaper than current
                    # but has a higher float (up to max_actual_float)
                    best = pm.get_best_buy_with_float(
                        nm,
                        target_wear=None, # Any wear that fits the float
                        max_float=max_actual_float,
                        exclude_stattrak=not bool(is_stattrak),
                        require_stattrak=bool(is_stattrak),
                    )
                    
                    if best:
                        new_price, new_flt, new_wear, src = best
                        if float(new_price) < float(skin.get('price', 0.0)) - 0.01:
                            # Found a cheaper one!
                            current_skins[i].update({
                                'price': float(new_price),
                                'float': float(new_flt),
                                'wear': str(new_wear),
                                'buy_source': str(src)
                            })
                            changed = True
            except Exception:
                continue

        return current_skins if changed else None

    def find_target_hunting_optimized(
        self,
        *,
        max_results: int = 20,
        max_investment: Optional[float] = None,
        is_stattrak: bool = False,
        input_rarities: Optional[List[str]] = None,
        min_roi_pct: float = 5.0,
        min_profit_probability: float = 0.40,
    ) -> List[Dict]:
        if input_rarities is None:
            input_rarities = ['Mil-Spec', 'Restricted', 'Classified']

        try:
            all_collections = list(self.database.list_collections())
        except Exception:
            all_collections = []

        results: List[Dict] = []

        splits = [(10, 0), (9, 1), (8, 2), (7, 3), (6, 4), (5, 5)]

        for input_rarity in input_rarities:
            diag = {
                'targets': 0,
                'no_target_items': 0,
                'no_fillers': 0,
                'bad_eval': 0,
                'ev_lt_cost': 0,
                'roi_fail': 0,
                'pp_fail': 0,
                'score_le0': 0,
                'accepted': 0,
            }
            scored = []
            for c in all_collections:
                s = self._collection_score(c, input_rarity, is_stattrak=is_stattrak)
                if s is None:
                    continue
                scored.append((float(s), c))
            scored.sort(reverse=True)

            # pre-filter: keep only the top collections by average outcome value
            top_targets = [c for _, c in scored[: max(10, int(len(scored) * 0.25))]]
            filler_pool = [c for _, c in scored[: max(30, int(len(scored) * 0.60))]]

            for target_c in top_targets:
                diag['targets'] += 1
                best_wear = self._best_target_wear(target_c, input_rarity, is_stattrak=is_stattrak)
                max_float = self._wear_to_max_float(best_wear)
                effective_max_float = max(0.15, float(max_float or 0.0))

                # candidate filler collections: prefer high score and low next-grade outcomes count
                filler_candidates = []
                for c in filler_pool:
                    if c == target_c:
                        continue
                    outcomes_count = self._get_next_grade_skins_count(c, input_rarity, is_stattrak=is_stattrak)
                    if outcomes_count <= 0:
                        continue
                    s = self._collection_score(c, input_rarity, is_stattrak=is_stattrak)
                    if s is None:
                        continue
                    filler_candidates.append((int(outcomes_count), -float(s), c))
                filler_candidates.sort()
                filler_candidates = filler_candidates[:50]

                # target candidates list for local search
                target_items = self._get_candidate_inputs(
                    target_c,
                    input_rarity,
                    is_stattrak=is_stattrak,
                    max_float=effective_max_float,
                    limit=60,
                )
                if len(target_items) < 5:
                    diag['no_target_items'] += 1
                    continue

                for target_cnt, filler_cnt in splits:
                    if target_cnt <= 0:
                        continue
                    if len(target_items) < int(target_cnt):
                        continue

                    # baseline: cheapest target items
                    base_target = list(target_items[: int(target_cnt)])

                    # choose filler collection + items
                    best_contract = None
                    best_eval = None
                    best_score = None

                    filler_collection_choices = [None] if filler_cnt == 0 else [t[2] for t in filler_candidates[:15]]

                    for filler_c in filler_collection_choices:
                        fillers = []
                        if filler_cnt > 0:
                            if not filler_c:
                                continue
                            fillers = self._get_fillers_from_collection(
                                filler_c,
                                input_rarity,
                                int(max(80, filler_cnt * 30)),
                                is_stattrak,
                                target_float_threshold=float(effective_max_float),
                                max_price=None,
                            )
                            if len(fillers) < int(filler_cnt):
                                diag['no_fillers'] += 1
                                continue
                            fillers = list(fillers[: int(filler_cnt)])

                        contract = base_target + fillers
                        if len(contract) != 10:
                            continue

                        ev = self._evaluate_contract_cached(contract, target_c, is_stattrak=is_stattrak)

                        # strict filters
                        input_cost = float(ev.get('input_cost') or 0.0)
                        expected_output = float(ev.get('expected_output') or 0.0)
                        roi = float(ev.get('roi') or 0.0)
                        pp = float(ev.get('profit_probability') or 0.0)
                        if input_cost <= 0:
                            diag['bad_eval'] += 1
                            continue
                        if max_investment is not None and input_cost > float(max_investment):
                            continue
                        if expected_output + 1e-9 < input_cost:
                            diag['ev_lt_cost'] += 1
                            continue
                        if roi + 1e-9 < float(min_roi_pct):
                            diag['roi_fail'] += 1
                            continue
                        if pp + 1e-12 < float(min_profit_probability):
                            diag['pp_fail'] += 1
                            continue

                        current = list(contract)
                        current_eval = dict(ev)

                        # liquidity heuristic: fewer outcomes in target collection -> better
                        target_outcomes = self._get_next_grade_skins_count(target_c, input_rarity, is_stattrak=is_stattrak)
                        target_outcomes = max(1, int(target_outcomes))
                        liquidity_factor = 1.0 / float(target_outcomes)

                        profit = float(current_eval.get('net_profit') or 0.0)
                        chance = float(current_eval.get('profit_probability') or 0.0)
                        contract_score = profit * chance * float(liquidity_factor)
                        if contract_score <= 0:
                            diag['score_le0'] += 1
                            continue

                        out_prob = float(current_eval.get('output_probability') or 0.0)
                        avg_norm_val = float(current_eval.get('average_normalized_float') or 0.0)
                        # Determine expected wear from target skin's out_float
                        target_skin_data = self.database.get_skin_by_name(str(current_best_out.get('name') or ''))
                        if target_skin_data:
                            _out_f = avg_norm_val * (float(target_skin_data.max_float) - float(target_skin_data.min_float)) + float(target_skin_data.min_float)
                            expected_wear = self._determine_wear_from_float(_out_f)
                        else:
                            expected_wear = self._determine_wear_from_float(avg_norm_val)

                        out_name = None
                        out_price = 0.0
                        outs = self._get_possible_outputs(target_c, input_rarity, average_float=avg_float_val, is_stattrak=is_stattrak)
                        for o in outs:
                            p = self._cached_get_price(
                                o['name'],
                                target_wear=best_wear,
                                exclude_stattrak=not is_stattrak,
                                require_stattrak=bool(is_stattrak),
                                strict_name_match=False,
                                allow_refresh=False,
                            )
                            if p and float(p) > out_price:
                                out_price = float(p)
                                out_name = o['name']

                        item = dict(current_eval)
                        item.update({
                            'target_collection': target_c,
                            'is_stattrak': bool(is_stattrak),
                            'input_skins': current,
                            'main_skins_count': int(target_cnt),
                            'filler_skins_count': int(filler_cnt),
                            'hunt_output': out_name,
                            'hunt_output_price': float(out_price),
                            'hunt_target_wear': best_wear,
                            'hunt_expected_wear': expected_wear,
                            'hunt_input_rarity': self._normalize_rarity(input_rarity),
                            'hunt_filler_collection': filler_c,
                            'hunt_filler_outcomes': self._get_next_grade_skins_count(filler_c, input_rarity, is_stattrak=is_stattrak) if filler_c else None,
                            'contract_score': float(contract_score),
                            'liquidity_factor': float(liquidity_factor),
                            'output_probability': float(out_prob),
                        })

                        if best_contract is None or float(item.get('contract_score') or 0.0) > float(best_score or 0.0):
                            best_contract = item
                            best_eval = item
                            best_score = float(item.get('contract_score') or 0.0)

                    if best_eval is not None:
                        results.append(best_eval)
                        diag['accepted'] += 1

            try:
                self._logger.info(
                    "Target Hunting optimized diag (%s): targets=%d no_target_items=%d no_fillers=%d bad_eval=%d ev_lt_cost=%d roi_fail=%d pp_fail=%d score_le0=%d accepted=%d",
                    self._normalize_rarity(input_rarity),
                    int(diag.get('targets') or 0),
                    int(diag.get('no_target_items') or 0),
                    int(diag.get('no_fillers') or 0),
                    int(diag.get('bad_eval') or 0),
                    int(diag.get('ev_lt_cost') or 0),
                    int(diag.get('roi_fail') or 0),
                    int(diag.get('pp_fail') or 0),
                    int(diag.get('score_le0') or 0),
                    int(diag.get('accepted') or 0),
                )
            except Exception:
                pass

        # de-duplicate by input set
        uniq = {}
        for r in results:
            sig = (r.get('target_collection'), r.get('hunt_input_rarity'), tuple(sorted(s.get('name') for s in (r.get('input_skins') or []))))
            prev = uniq.get(sig)
            if prev is None or float(r.get('contract_score') or 0.0) > float(prev.get('contract_score') or 0.0):
                uniq[sig] = r
        results = list(uniq.values())

        results.sort(key=lambda x: float(x.get('contract_score') or 0.0), reverse=True)
        return results[: int(max_results)]

    def _compute_risk_metrics(self, contract_skins: List[Dict], *, is_stattrak: bool) -> Dict[str, float]:
        input_cost = sum(float(s.get('price') or 0.0) for s in contract_skins)
        if input_cost <= 0:
            return {
                'fail_probability': 0.0,
                'profit_probability': 0.0,
                'avg_fail_value_after_fee': 0.0,
                'min_outcome_after_fee': 0.0,
                'worst_case_loss_pct': 0.0,
                'expected_loss_on_fail': 0.0,
                'risk_ratio': 0.0,
            }

        outcomes = self.calculate_contract_outcomes_details(contract_skins, is_stattrak=is_stattrak)
        if not outcomes:
            return {
                'fail_probability': 0.0,
                'profit_probability': 0.0,
                'avg_fail_value_after_fee': 0.0,
                'min_outcome_after_fee': 0.0,
                'worst_case_loss_pct': 1.0,
                'expected_loss_on_fail': float(input_cost),
                'risk_ratio': float('inf'),
            }

        fee_mult = 1.0 - float(self.market_fee)
        fail_prob = 0.0
        profit_prob = 0.0
        fail_value_weighted = 0.0
        min_after_fee = None

        for o in outcomes:
            prob = float(o.get('probability') or 0.0)
            price = float(o.get('price') or 0.0)
            after_fee = price * fee_mult
            if min_after_fee is None or after_fee < float(min_after_fee):
                min_after_fee = float(after_fee)
            if after_fee >= float(input_cost):
                profit_prob += prob
            else:
                fail_prob += prob
                fail_value_weighted += after_fee * prob

        avg_fail_after_fee = (fail_value_weighted / fail_prob) if fail_prob > 1e-12 else 0.0
        expected_loss_on_fail = float(input_cost) - float(avg_fail_after_fee)
        min_after_fee = float(min_after_fee) if min_after_fee is not None else 0.0
        worst_case_loss_pct = (float(input_cost) - float(min_after_fee)) / float(input_cost) if float(input_cost) > 0 else 0.0
        denom = max(float(avg_fail_after_fee), 1e-9)
        risk_ratio = float(input_cost) / denom
        return {
            'fail_probability': float(fail_prob),
            'profit_probability': float(profit_prob),
            'avg_fail_value_after_fee': float(avg_fail_after_fee),
            'min_outcome_after_fee': float(min_after_fee),
            'worst_case_loss_pct': float(worst_case_loss_pct),
            'expected_loss_on_fail': float(expected_loss_on_fail),
            'risk_ratio': float(risk_ratio),
        }

    def _is_golden_filler_present(
        self,
        fillers: List[Dict],
        *,
        input_rarity: str,
        is_stattrak: bool,
        target_collection: Optional[str] = None,
    ) -> bool:
        if not fillers:
            return False

        filler_prices = [float(s.get('price') or 0.0) for s in fillers if float(s.get('price') or 0.0) > 0.0]
        if not filler_prices:
            return False
        cheapest = min(filler_prices)
        if cheapest <= 0:
            return False

        for s in fillers:
            p = float(s.get('price') or 0.0)
            if p <= 0:
                continue
            if p < (cheapest * float(self._golden_filler_price_multiplier)):
                continue

            c = s.get('collection')
            if not c:
                continue
            if target_collection and c == target_collection:
                continue

            outcomes_count = self._get_next_grade_skins_count(c, input_rarity, is_stattrak)
            if int(outcomes_count) >= int(self._golden_filler_min_outcomes):
                return True

        return False

    def _compute_collection_potential(
        self,
        collection_name: str,
        input_rarity: str,
        is_stattrak: bool,
        wears: Optional[List[str]] = None,
    ) -> Optional[Dict[str, float]]:
        try:
            input_rarity = self._normalize_rarity(input_rarity)
        except Exception:
            return None

        if wears is None:
            wears = ["Factory New", "Minimal Wear"]

        cheapest_inputs = self._get_main_skins(
            collection_name,
            count=3,
            is_stattrak=is_stattrak,
            rarity=input_rarity,
            max_float=1.0,
        )
        if not cheapest_inputs:
            return None

        input_prices = [float(s.get('price')) for s in cheapest_inputs if s.get('price')]
        if not input_prices:
            return None

        avg_input_price = sum(input_prices) / max(1, len(input_prices))
        if avg_input_price <= 0:
            return None

        max_output_price = 0.0
        outputs = self._get_possible_outputs(collection_name, input_rarity, average_float=None, is_stattrak=is_stattrak)
        if not outputs:
            return None

        for wear in wears:
            for out in outputs:
                p = self._cached_get_price(
                    out['name'],
                    target_wear=wear,
                    exclude_stattrak=not is_stattrak,
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=False,
                    allow_refresh=False,
                )
                if p and float(p) > max_output_price:
                    max_output_price = float(p)

        if max_output_price <= 0:
            return None

        return {
            'avg_input_price': float(avg_input_price),
            'max_output_price': float(max_output_price),
            'output_multiplier': float(max_output_price) / float(avg_input_price),
        }

    def _estimate_float_from_wear(self, wear: Optional[str]) -> Optional[float]:
        if not wear:
            return None
        w = str(wear)
        if w == 'Factory New':
            return 0.0699
        if w == 'Minimal Wear':
            return 0.1499
        if w == 'Field-Tested':
            return 0.3799
        if w == 'Well-Worn':
            return 0.4499
        if w == 'Battle-Scarred':
            return 0.9999
        return None

    def _wear_for_max_float(self, max_float: Optional[float]) -> Optional[str]:
        if max_float is None:
            return None
        try:
            mf = float(max_float)
        except Exception:
            return None
        # Выбираем "лучшее" качество, которое гарантированно < max_float
        # Используем < вместо <=, так как границы wear эксклюзивные
        if mf < 0.07:
            return 'Factory New'
        if mf < 0.15:
            return 'Minimal Wear'
        if mf < 0.38:
            return 'Field-Tested'
        if mf < 0.45:
            return 'Well-Worn'
        return None

    def _cached_get_price_with_float(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = True,
        allow_refresh: bool = False,
    ) -> Optional[Tuple[float, float, str]]:
        mf = None if max_float is None else round(float(max_float), 4)
        key = (skin_name, target_wear, mf, bool(exclude_stattrak), bool(require_stattrak), bool(strict_name_match), bool(allow_refresh))
        with self._lock_price_float:
            if key in self._memo_price_with_float:
                return self._memo_price_with_float[key]
        val = self.price_manager.get_price_with_float(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )

        # If strict name match fails, retry with relaxed matching once and memoize under both keys.
        if not val and bool(strict_name_match):
            key_relaxed = (skin_name, target_wear, mf, bool(exclude_stattrak), bool(require_stattrak), False, bool(allow_refresh))
            with self._lock_price_float:
                cached_relaxed = self._memo_price_with_float.get(key_relaxed)
            if cached_relaxed is not None:
                val = cached_relaxed
            else:
                val = self.price_manager.get_price_with_float(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    strict_name_match=False,
                    allow_refresh=allow_refresh,
                )
                with self._lock_price_float:
                    self._memo_price_with_float[key_relaxed] = val

        with self._lock_price_float:
            self._memo_price_with_float[key] = val
        return val

    def _cached_get_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = True,
        allow_refresh: bool = False,
    ) -> Optional[float]:
        mf = None if max_float is None else round(float(max_float), 4)
        key = (skin_name, target_wear, mf, bool(exclude_stattrak), bool(require_stattrak), bool(strict_name_match), bool(allow_refresh))
        with self._lock_price_float:
            if key in self._memo_price:
                return self._memo_price[key]
        val = self.price_manager.get_price(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )

        if (val is None or float(val) <= 0) and bool(strict_name_match):
            key_relaxed = (skin_name, target_wear, mf, bool(exclude_stattrak), bool(require_stattrak), False, bool(allow_refresh))
            with self._lock_price_float:
                cached_relaxed = self._memo_price.get(key_relaxed)
            if cached_relaxed is not None:
                val = cached_relaxed
            else:
                val = self.price_manager.get_price(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    strict_name_match=False,
                    allow_refresh=allow_refresh,
                )
                with self._lock_price_float:
                    self._memo_price[key_relaxed] = val

        with self._lock_price_float:
            self._memo_price[key] = val
        return val

    def clear_price_memoization(self) -> None:
        # Захват всех гранулярных lock в алфавитном порядке для предотвращения deadlock
        with self._lock_contract_eval:
            with self._lock_listings:
                with self._lock_main_skins:
                    with self._lock_next_grade:
                        with self._lock_price_float:
                            with self._lock_sell_price:
                                self._memo_price.clear()
                                self._memo_price_with_float.clear()
                                self._memo_listings.clear()
                                self._memo_effective_sell_price.clear()
                                self._memo_contract_eval.clear()
                                self._memo_contract_craftability.clear()
                                self._memo_collection_avg_outcome_price.clear()
                                self._memo_collection_score.clear()
                                self._memo_collection_imbalance.clear()
        self._memo_possible_outputs.clear()
        self._memo_contract_target_prob.clear()
        self._memo_contract_max_output.clear()
    
    def _normalize_rarity(self, rarity_name: Optional[str]) -> Optional[str]:
        if not rarity_name:
            return rarity_name
        try:
            return self.database._normalize_rarity(rarity_name)
        except Exception:
            return rarity_name

    def calculate_mixed_contract_probabilities(self, input_skins: List[str]) -> List[SkinProbability]:
        """
        Расчет вероятностей для контракта с учетом строгой проверки коллекций
        
        Args:
            input_skins: список имен скинов
            
        Returns:
            список вероятностей для всех возможных исходов
        """
        # Получаем информацию о входных скинах
        input_skin_data = []
        for skin_name in input_skins:
            skin = self.database.get_skin_by_name(skin_name)
            if skin:
                input_skin_data.append(skin)
        
        if not input_skin_data:
            return []
        
        # Определяем основную коллекцию (самая частая)
        collection_counts = defaultdict(int)
        for skin in input_skin_data:
            collection_counts[skin.collection] += 1
        
        if not collection_counts:
            return []
        
        # Берем коллекцию с максимальным количеством скинов
        main_collection = max(collection_counts, key=collection_counts.get)
        
        # Проверяем, что все скины из одной коллекции (чистый контракт)
        if len(collection_counts) > 1:
            # Смешанный контракт - уменьшаем вероятности
            main_collection_skins = [s for s in input_skin_data if s.collection == main_collection]
            if len(main_collection_skins) < 5:  # Минимум 5 скинов из одной коллекции
                return []
        
        # Считаем количество скинов по редкостям в основной коллекции
        rarity_counts = defaultdict(int)
        for skin in input_skin_data:
            if skin.collection == main_collection:
                # Проверяем, что это следующий уровень редкости
                if self._is_next_rarity(skin.rarity, rarity_counts):
                    rarity_counts[skin.rarity] += 1
        
        # Получаем все возможные исходы из основной коллекции
        outcomes = []
        for skin in self.database.skins.values():
            if skin.collection == main_collection:
                # Проверяем, что это следующий уровень редкости
                if self._is_next_rarity(skin.rarity, rarity_counts):
                    outcomes.append(skin)
        
        if not outcomes:
            return []
        
        # Рассчитываем вероятности для каждого исхода
        probabilities = []
        total_input_skins = len([s for s in input_skin_data if s.collection == main_collection])
        
        for outcome in outcomes:
            # Формула вероятности: (N_coll / 10) * (1 / M_coll)
            # N_coll - количество скинов того же грейда в контракте
            # M_coll - количество скинов того же грейда в коллекции
            
            n_coll = rarity_counts.get(outcome.rarity, 0)
            m_coll = len([s for s in self.database.skins.values() 
                         if s.collection == main_collection and s.rarity == outcome.rarity])
            
            if m_coll > 0:
                probability = (n_coll / 10) * (1 / m_coll)
                probabilities.append(SkinProbability(
                    skin_name=outcome.name,
                    collection=outcome.collection,
                    rarity=outcome.rarity,
                    probability=probability
                ))
        
        return probabilities
    
    def calculate_wear_leap(self, input_skins: List[str]) -> Dict[str, float]:
        """
        Анализ перехода качества (Wear Leap)
        
        Args:
            input_skins: список имен входных скинов
            
        Returns:
            Dict с информацией о возможных переходах качества
        """
        # Получаем float информацию для входных скинов
        input_floats = []
        for skin_name in input_skins:
            price_info = self.price_manager.get_skin_price_with_float(skin_name, exclude_stattrak=True)
            if price_info:
                price, item_float, wear = price_info
                input_floats.append(item_float)
        
        if not input_floats:
            return {}
        
        # Рассчитываем средний float входных скинов
        avg_input_float = sum(input_floats) / len(input_floats)
        
        # Определяем возможное качество результата (CS2 алгоритм)
        # Используем <= для верхней границы (включительно)
        quality_thresholds = {
            "Factory New": 0.07,
            "Minimal Wear": 0.15,
            "Field-Tested": 0.38,
            "Well-Worn": 0.45,  # Исправлено: было 0.44
            "Battle-Scarred": 1.0
        }
        
        result_quality = "Battle-Scarred"
        for quality, threshold in quality_thresholds.items():
            if avg_input_float <= threshold:
                result_quality = quality
                break
        
        return {
            "avg_input_float": avg_input_float,
            "result_quality": result_quality,
            "can_be_fn": avg_input_float <= 0.07,  # Исправлено: <= вместо <
            "can_be_mw": avg_input_float <= 0.15,  # Исправлено: <= вместо <
            "quality_leap": self._calculate_quality_leap(input_floats, avg_input_float)
        }
    
    def _calculate_quality_leap(self, input_floats: List[float], avg_float: float) -> str:
        """Рассчитывает тип перехода качества (CS2 алгоритм).
        
        Используем <= для верхней границы (включительно).
        """
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
    
    def _is_next_rarity(self, target_rarity: str, input_rarity_counts: Dict[str, int]) -> bool:
        rarity_hierarchy = {
            "Consumer": 0,
            "Mil-Spec": 1, 
            "Restricted": 2,
            "Classified": 3,
            "Covert": 4
        }
        
        # Находим максимальную редкость входных скинов
        max_input_level = 0
        for rarity in input_rarity_counts:
            if rarity in rarity_hierarchy:
                max_input_level = max(max_input_level, rarity_hierarchy[rarity])
        
        # Целевая редкость должна быть на 1 уровень выше
        target_level = rarity_hierarchy.get(target_rarity, -1)
        return target_level == max_input_level + 1
    
    def _get_possible_outcomes(self, collection_name: str, min_rarity: str) -> List[SkinData]:
        """Получить возможные исходы для коллекции с грейдом >= min_rarity"""
        collection = self.database.get_collection(collection_name)
        if not collection:
            return []
        
        min_level = self.rarities_hierarchy.get(min_rarity, 0)
        outcomes = []
        
        for skin in collection.skins:
            normalized_rarity = self.database._normalize_rarity(skin.rarity)
            skin_level = self.rarities_hierarchy.get(normalized_rarity, 0)
            if skin_level >= min_level:
                outcomes.append(skin)
        
        return outcomes
    
    def find_milspec_to_restricted_contracts(self) -> List[ContractResult]:
        """
        Специфичный поиск: Mil-Spec -> Restricted с оптимальными филлерами
        """
        results = []
        
        # Находим все Mil-Spec скины с ценами
        all_milspec_skins = self.database.get_skins_by_rarity("Mil-Spec")
        milspec_names = [skin.name for skin in all_milspec_skins]

        self._logger.info("Всего найдено Mil-Spec скинов: %s", int(len(milspec_names)))
        
        milspec_prices = self.price_manager.market_client.get_multiple_prices(milspec_names)
        priced_milspec = [(name, price) for name, price in milspec_prices.items() if price and price > 0]
        priced_milspec.sort(key=lambda x: x[1])  # от дешевых к дорогим

        self._logger.info("Mil-Spec с ценами: %s", int(len(priced_milspec)))
        self._logger.info("Дешевые филлеры:")
        for i, (name, price) in enumerate(priced_milspec[:7]):
            self._logger.info("  %s. %s: $%.2f", int(i + 1), str(name), float(price))
        
        # Если недостаточно скинов с ценами, выходим
        if len(priced_milspec) < 7:
            self._logger.info("Недостаточно Mil-Spec скинов с ценами для создания филлеров")
            return results
        
        # Берем 7 самых дешевых как филлеры
        filler_skins = [skin[0] for skin in priced_milspec[:7]]
        
        # Ищем коллекции с Mil-Spec и Restricted
        milspec_collections = self.database.get_collections_with_rarity("Mil-Spec")
        
        for collection_name in milspec_collections:
            collection = self.database.get_collection(collection_name)
            if not collection:
                continue
            
            # Проверяем, есть ли в коллекции Restricted скины
            restricted_skins = self.database.get_skins_by_rarity("Restricted", collection_name)
            if not restricted_skins:
                continue
            
            # Находим Mil-Spec скины в этой коллекции
            collection_milspec = self.database.get_skins_by_rarity("Mil-Spec", collection_name)
            collection_milspec_names = [skin.name for skin in collection_milspec]
            collection_milspec_prices = self.price_manager.market_client.get_multiple_prices(collection_milspec_names)

            self._logger.info(
                "В коллекции %s найдено %s Mil-Spec скинов",
                str(collection_name),
                int(len(collection_milspec)),
            )
            self._logger.info(
                "С ценами: %s",
                int(len([p for p in collection_milspec_prices.values() if p])),
            )
            
            # Берем 3 самых дорогих
            expensive_skins = [(name, price) for name, price in collection_milspec_prices.items() if price and price > 0]
            expensive_skins.sort(key=lambda x: x[1], reverse=True)

            self._logger.info("Дорогих скинов в %s: %s", str(collection_name), int(len(expensive_skins)))
            
            for i in range(min(3, len(expensive_skins))):
                contract_skins = filler_skins + [expensive_skins[i][0]]
                
                # Добиваем еще 2 скинами из той же коллекции если нужно
                if len(contract_skins) < 10:
                    remaining_expensive = [skin[0] for skin in expensive_skins[i+1:i+3]]
                    contract_skins.extend(remaining_expensive[:10-len(contract_skins)])
                
                if len(contract_skins) == 10:
                    self._logger.info(
                        "Пробую контракт для %s с %s",
                        str(collection_name),
                        str(expensive_skins[i][0]),
                    )
                    result = self._calculate_contract_result(contract_skins, collection_name)
                    if result and result.roi_percentage > -50:  # фильтруем очень убыточные
                        results.append(result)
                        self._logger.info("Добавлен результат: ROI %.2f%%", float(result.roi_percentage))
                    else:
                        self._logger.info("Результат отфильтрован или None")

        self._logger.info("Всего найдено результатов: %s", int(len(results)))
        
        # Сортируем по ROI
        results.sort(key=lambda x: x.roi_percentage, reverse=True)
        return results[:10]
    
    def _calculate_contract_result(self, input_skins: List[str], target_collection: str) -> Optional[ContractResult]:
        """Рассчитать результат с реальными float данными и анализом Wear Leap"""
        # Получаем цены входных скинов с учетом float
        input_prices = {}
        total_cost = 0.0
        
        for skin_name in input_skins:
            # Ищем самую дешевую цену для каждого скина
            price_info = self.price_manager.get_skin_price_with_float(skin_name, exclude_stattrak=True)
            if price_info:
                price, item_float, wear = price_info
                input_prices[skin_name] = price
                total_cost += price
        
        if total_cost == 0:
            return None
        
        # Анализ Wear Leap
        wear_leap_info = self.calculate_wear_leap(input_skins)
        
        # Рассчитываем вероятности
        probabilities = self.calculate_mixed_contract_probabilities(input_skins)
        
        # Фильтруем вероятности для целевой коллекции
        target_probs = [p for p in probabilities if p.collection == target_collection]
        
        if not target_probs:
            return None
        
        # Находим самый вероятный и самый ценный исход
        best_outcome = None
        best_expected_value = 0
        
        for prob in target_probs:
            # Ищем цену с учетом float для целевого скина
            float_info = self.database.get_float_info_for_skin(prob.skin_name)
            max_float_for_fn = float_info['max_avg_float_for_fn'] if float_info else 0.07
            
            # Используем Wear Leap информацию для выбора качество
            target_wear = None
            if wear_leap_info.get("can_be_fn"):
                target_wear = "Factory New"
            elif wear_leap_info.get("can_be_mw"):
                target_wear = "Minimal Wear"
            
            price_info = self.price_manager.get_skin_price_with_float(
                prob.skin_name, 
                max_float=max_float_for_fn,
                target_wear=target_wear,
                exclude_stattrak=True
            )
            if price_info:
                price, item_float, wear = price_info
                expected_value = price * prob.probability
                if expected_value > best_expected_value:
                    best_expected_value = expected_value
                    best_outcome = prob
        
        if not best_outcome:
            return None
        
        # Рассчитываем общую ожидаемую прибыль с учетом 15% комиссии
        total_expected_value = 0
        for p in probabilities:
            float_info = self.database.get_float_info_for_skin(p.skin_name)
            max_float_for_fn = float_info['max_avg_float_for_fn'] if float_info else 0.07
            
            # Используем Wear Leap информацию
            target_wear = None
            if wear_leap_info.get("can_be_fn"):
                target_wear = "Factory New"
            elif wear_leap_info.get("can_be_mw"):
                target_wear = "Minimal Wear"
            
            price_info = self.price_manager.get_skin_price_with_float(
                p.skin_name, 
                max_float=max_float_for_fn,
                target_wear=target_wear,
                exclude_stattrak=True
            )
            if price_info:
                price, item_float, wear = price_info
                total_expected_value += price * p.probability
        
        # Учитываем комиссию рынка при продаже
        net_expected_value = total_expected_value * (1 - self.market_fee)
        roi = ((net_expected_value - total_cost) / total_cost) * 100 if total_cost > 0 else 0
        
        # Float информация
        float_info = self.database.get_float_info_for_skin(best_outcome.skin_name)
        max_average_float = float_info['max_avg_float_for_fn'] if float_info else 0.07
        
        return ContractResult(
            target_skin=best_outcome.skin_name,
            probability=best_outcome.probability * 100,
            investment_cost=total_cost,
            expected_value=net_expected_value,
            roi_percentage=roi,
            collection_name=target_collection,
            input_skins=input_skins,
            max_average_float=max_average_float,
            wear_leap_info=wear_leap_info  # Добавляем информацию о Wear Leap
        )
    
    def _compute_cross_collection_contracts(self) -> List[Dict]:
        contracts = []
        all_collections = list(self.database.list_collections())

        # Ensure price cache is available before starting background computation.
        # This should be fast if a valid local cache exists; otherwise it will load prices once.
        try:
            self.price_manager.refresh_prices(force_refresh=False)
        except Exception:
            # Price refresh failure should not crash the whole contracts compute.
            pass

        for is_stattrak in [False, True]:
            mode = 'ST' if is_stattrak else 'NON-ST'
            self._logger.info(
                "Cross-contracts compute started (%s): %s collections",
                mode,
                len(all_collections),
            )
            started_mode_ts = time.time()
            processed = 0
            contracts.extend(
                self._generate_contracts_by_type(all_collections, is_stattrak, max_investment=None)
            )
            processed = len(all_collections)
            self._logger.info(
                "Cross-contracts compute finished (%s): processed %s collections in %.2fs",
                mode,
                processed,
                time.time() - started_mode_ts,
            )

        contracts.sort(key=lambda x: x['roi'], reverse=True)
        return contracts

    def refresh_cross_collection_cache(self, blocking: bool = False) -> None:
        def _job():
            start_ts = time.time()
            self._logger.info("Cross-contracts cache refresh started")
            try:
                computed = self._compute_cross_collection_contracts()
                duration = time.time() - start_ts
                with self._cross_contracts_cache_lock:
                    self._cross_contracts_cache = computed
                    self._cross_contracts_cache_ts = time.time()
                    self._cross_contracts_cache_last_success_ts = self._cross_contracts_cache_ts
                    self._cross_contracts_cache_last_duration_seconds = duration
                    self._cross_contracts_cache_last_error = ""

                self._logger.info(
                    "Cross-contracts cache refresh finished: %s contracts in %.2fs",
                    len(computed),
                    duration,
                )
            except Exception as e:
                duration = time.time() - start_ts
                with self._cross_contracts_cache_lock:
                    if self._cross_contracts_cache is None:
                        self._cross_contracts_cache = []
                        self._cross_contracts_cache_ts = time.time()
                    self._cross_contracts_cache_last_duration_seconds = duration
                    self._cross_contracts_cache_last_error = f"{type(e).__name__}: {e}"

                self._logger.exception(
                    "Cross-contracts cache refresh failed after %.2fs: %s",
                    duration,
                    e,
                )
            finally:
                with self._cross_contracts_cache_lock:
                    self._cross_contracts_cache_refreshing = False

        with self._cross_contracts_cache_lock:
            if self._cross_contracts_cache_refreshing:
                return
            self._cross_contracts_cache_refreshing = True
            self._cross_contracts_cache_refresh_started_ts = time.time()

        self._logger.info(
            "Cross-contracts cache refresh scheduled (blocking=%s)",
            blocking,
        )

        if blocking:
            _job()
        else:
            t = threading.Thread(target=_job, daemon=True)
            t.start()

    def find_cross_collection_contracts(self, max_investment: float = None) -> List[Dict]:
        now = time.time()

        with self._cross_contracts_cache_lock:
            cache = self._cross_contracts_cache
            cache_age = now - self._cross_contracts_cache_ts if self._cross_contracts_cache_ts else None
            refreshing = self._cross_contracts_cache_refreshing

        # Если кэша нет — первый запуск: считаем синхронно (иначе нечего показывать)
        if cache is None:
            self.refresh_cross_collection_cache(blocking=True)
            with self._cross_contracts_cache_lock:
                cache = self._cross_contracts_cache or []

        # Если кэш устарел — обновляем в фоне, но возвращаем текущий кэш сразу
        if cache_age is None or cache_age > self._cross_contracts_cache_ttl_seconds:
            if not refreshing:
                self.refresh_cross_collection_cache(blocking=False)

        results = cache
        if max_investment is not None:
            results = [c for c in results if c.get('input_cost', 0) <= max_investment]

        return results

    def get_cross_contracts_cache_info(self) -> Dict:
        now = time.time()
        with self._cross_contracts_cache_lock:
            cache = self._cross_contracts_cache
            ts = self._cross_contracts_cache_ts
            refreshing = self._cross_contracts_cache_refreshing
            refresh_started_ts = self._cross_contracts_cache_refresh_started_ts
            last_success_ts = self._cross_contracts_cache_last_success_ts
            last_duration = self._cross_contracts_cache_last_duration_seconds
            last_error = self._cross_contracts_cache_last_error

        return {
            'has_cache': cache is not None,
            'count': len(cache) if cache is not None else 0,
            'refreshing': bool(refreshing),
            'age_seconds': (now - ts) if ts else None,
            'refresh_running_seconds': (now - refresh_started_ts) if (refreshing and refresh_started_ts) else None,
            'last_success_age_seconds': (now - last_success_ts) if last_success_ts else None,
            'last_duration_seconds': last_duration if last_duration else None,
            'last_error': last_error or None,
        }

    def hunt_targets(
        self,
        wears: Optional[List[str]] = None,
        min_edge: float = 1.15,
        top_n: int = 30,
    ) -> List[Dict]:
        if wears is None:
            wears = ["Factory New", "Minimal Wear"]

        targets: List[Dict] = []
        all_collections = self.database.list_collections()

        for is_stattrak in [False, True]:
            mode = 'ST' if is_stattrak else 'NON-ST'
            for input_rarity in ["Mil-Spec", "Restricted", "Classified"]:
                for collection_name in all_collections:
                    # Оцениваем дешевизну входа по самой дешевой цене в коллекции
                    cheapest_inputs = self._get_main_skins(
                        collection_name,
                        count=3,
                        is_stattrak=is_stattrak,
                        rarity=input_rarity,
                    )
                    if not cheapest_inputs:
                        continue

                    input_prices = [s.get('price') for s in cheapest_inputs if s.get('price')]
                    if not input_prices:
                        continue
                    avg_input_price = sum(input_prices) / len(input_prices)
                    est_input_cost = avg_input_price * 10

                    # Ищем самый дорогой выход по каждому wear
                    for wear in wears:
                        outputs = self._get_possible_outputs(collection_name, input_rarity, average_float=None, is_stattrak=is_stattrak)
                        if not outputs:
                            continue

                        max_output_price = 0.0
                        max_output_name = None
                        for out in outputs:
                            p = self.price_manager.get_price(
                                out['name'],
                                target_wear=wear,
                                exclude_stattrak=not is_stattrak,
                                require_stattrak=bool(is_stattrak),
                            )
                            if p and p > max_output_price:
                                max_output_price = p
                                max_output_name = out['name']

                        if not max_output_price or est_input_cost <= 0:
                            continue

                        edge = (max_output_price * (1.0 - float(self.market_fee))) / est_input_cost
                        if edge < min_edge:
                            continue

                        targets.append({
                            'collection': collection_name,
                            'input_rarity': self._normalize_rarity(input_rarity),
                            'is_stattrak': is_stattrak,
                            'target_wear': wear,
                            'edge': edge,
                            'est_input_cost': est_input_cost,
                            'max_output_price': max_output_price,
                            'max_output_name': max_output_name,
                            'mode': mode,
                        })

        targets.sort(key=lambda x: x.get('edge', 0.0), reverse=True)
        return targets[:top_n]

    def find_hunted_contracts(
        self,
        max_targets: int = 10,
        wears: Optional[List[str]] = None,
        min_edge: float = 1.15,
        max_investment: Optional[float] = None,
    ) -> List[Dict]:
        targets = self.hunt_targets(wears=wears, min_edge=min_edge, top_n=max_targets)
        contracts: List[Dict] = []

        for t in targets:
            target_collection = t['collection']
            input_rarity = t['input_rarity']
            is_stattrak = t['is_stattrak']

            potential = self._compute_collection_potential(
                target_collection,
                input_rarity,
                is_stattrak,
                wears=wears,
            )
            if not potential or float(potential.get('output_multiplier') or 0.0) <= float(self._output_multiplier_threshold):
                continue

            for main_count in [1, 2, 3]:
                filler_count = 10 - main_count
                main_skins = self._get_main_skins(
                    target_collection,
                    main_count,
                    is_stattrak,
                    rarity=input_rarity,
                    max_float=1.0,
                )
                if len(main_skins) < main_count:
                    continue

                main_prices = [s.get('price') for s in main_skins if s.get('price')]
                avg_main_price = (sum(main_prices) / len(main_prices)) if main_prices else None

                min_main_price = min((float(p) for p in main_prices if p is not None), default=None)
                if min_main_price is None or min_main_price <= 0:
                    continue
                filler_price_cap = float(min_main_price) * float(self._filler_to_target_price_ratio)

                # Use normalized float (avg_norm) for wear constraints.
                float_targets = [0.07, 0.15]
                fillers: List[Dict] = []
                for target_avg in float_targets:
                    required_filler_threshold = min(target_avg, 1.0)

                    fillers = self._get_smart_fillers(
                        input_rarity,
                        filler_count,
                        target_collection,
                        is_stattrak,
                        target_float_threshold=required_filler_threshold,
                        max_price=filler_price_cap,
                    )
                    if len(fillers) >= filler_count:
                        candidate_contract = main_skins + fillers[:filler_count]
                        candidate_avg_norm = self._calculate_average_normalized_float(candidate_contract, is_stattrak)
                        if candidate_avg_norm <= (target_avg + 1e-9):
                            break
                        fillers = []

                if len(fillers) < filler_count:
                    continue

                contract_skins = main_skins + fillers[:filler_count]

                if self._is_golden_filler_present(
                    fillers[:filler_count],
                    input_rarity=input_rarity,
                    is_stattrak=is_stattrak,
                    target_collection=target_collection,
                ):
                    continue

                ceiling = float(t.get('max_output_price') or 0.0)
                if ceiling > 0.0:
                    if any(float(s.get('price') or 0.0) > ceiling for s in contract_skins):
                        continue

                used_collections = {s.get('collection') for s in contract_skins if s.get('collection')}
                ok = True
                for c in used_collections:
                    if c == target_collection:
                        continue
                    p2 = self._compute_collection_potential(c, input_rarity, is_stattrak, wears=wears)
                    if not p2 or float(p2.get('output_multiplier') or 0.0) <= float(self._output_multiplier_threshold):
                        ok = False
                        break
                if not ok:
                    continue

                if self._calculate_average_normalized_float(contract_skins, is_stattrak) > 0.15:
                    continue
                contract_data = self._calculate_contract_profit(
                    contract_skins, target_collection, is_stattrak
                )

                risk_metrics = self._compute_risk_metrics(contract_skins, is_stattrak=is_stattrak)
                risk_ratio = float(risk_metrics.get('risk_ratio') or 0.0)
                worst_case_loss_pct = float(risk_metrics.get('worst_case_loss_pct') or 0.0)
                if worst_case_loss_pct > float(self._max_worst_case_loss_pct):
                    continue
                if risk_ratio > float(self._max_risk_ratio):
                    roi = float(contract_data.get('roi') or 0.0)
                    pp = float(risk_metrics.get('profit_probability') or 0.0)
                    if not (roi >= float(self._risk_ratio_override_min_roi) and pp >= float(self._risk_ratio_override_min_profit_probability)):
                        continue

                if max_investment and contract_data['input_cost'] > max_investment:
                    continue

                contract_data.update({
                    'target_collection': target_collection,
                    'is_stattrak': is_stattrak,
                    'input_skins': contract_skins,
                    'main_skins_count': main_count,
                    'filler_skins_count': filler_count,
                    'hunt_output': t.get('max_output_name'),
                    'hunt_output_price': t.get('max_output_price'),
                    'hunt_target_wear': t.get('target_wear'),
                    'hunt_expected_wear': (lambda f_prime, name: (
                        (lambda skin: self._determine_wear_from_float(
                            f_prime * (float(skin.max_float) - float(skin.min_float)) + float(skin.min_float)
                        ) if skin else self._determine_wear_from_float(f_prime))
                        (self.database.get_skin_by_name(str(name or "")))
                    ))(float(contract_data.get('average_normalized_float') or 0.0), t.get('max_output_name')),
                    'hunt_input_rarity': input_rarity,
                    'hunt_filler_collection': "+".join(used_collections) if used_collections else None,
                    'hunt_filler_outcomes': (
                        min(
                            self._get_next_grade_skins_count(c, input_rarity, is_stattrak)
                            for c in used_collections
                            if c != target_collection
                        )
                        if any(c != target_collection for c in used_collections)
                        else None
                    ),
                    'risk_ratio': float(risk_metrics.get('risk_ratio') or 0.0),
                    'fail_probability': float(risk_metrics.get('fail_probability') or 0.0),
                    'avg_fail_value_after_fee': float(risk_metrics.get('avg_fail_value_after_fee') or 0.0),
                    'min_outcome_after_fee': float(risk_metrics.get('min_outcome_after_fee') or 0.0),
                    'worst_case_loss_pct': float(risk_metrics.get('worst_case_loss_pct') or 0.0),
                    'expected_loss_on_fail': float(risk_metrics.get('expected_loss_on_fail') or 0.0),
                })
                if float(contract_data.get('net_profit') or 0.0) > 0.0:
                    contracts.append(contract_data)

        contracts.sort(
            key=lambda x: (
                float(x.get('net_profit') or -1e9),
                float(x.get('roi') or -1e9),
                -float(x.get('input_cost') or 0.0),
            ),
            reverse=True,
        )
        return contracts

    def find_target_suite_contracts(
        self,
        target_collection: Optional[str] = None,
        *,
        desired_target_probability: float = 0.30,
        wears: Optional[List[str]] = None,
        max_investment: Optional[float] = None,
        max_filler_collections: int = 80,
        max_results: int = 30,
        is_stattrak: bool = False,
        input_rarities: Optional[List[str]] = None,
    ) -> List[Dict]:
        if wears is None:
            wears = ["Factory New", "Minimal Wear"]

        if input_rarities is None:
            input_rarities = ["Mil-Spec", "Restricted", "Classified"]

        try:
            all_collections = list(self.database.list_collections())
        except Exception:
            all_collections = []

        if target_collection is not None and target_collection not in set(all_collections):
            return []

        p = float(desired_target_probability)
        if p <= 0:
            p = 0.10
        if p > 0.40:
            p = 0.40

        core_count = int(round(p * 10.0))
        core_count = max(1, min(4, core_count))
        filler_count = 10 - core_count

        results: List[Dict] = []

        diag_samples: List[Dict] = []

        def _push_diag_sample(sample: Dict) -> None:
            try:
                if len(diag_samples) < 25:
                    diag_samples.append(sample)
            except Exception:
                pass

        skipped_no_outputs = 0
        skipped_no_output_prices = 0
        skipped_no_core = 0
        skipped_potential_fail = 0
        skipped_budget_total = 0
        skipped_no_core_prices = 0
        skipped_min_core_price = 0
        skipped_gap_fail = 0
        skipped_golden_filler = 0
        skipped_best_out_ceiling = 0
        skipped_suite_potential_fail = 0
        skipped_avg_float_check = 0
        skipped_worst_case_loss = 0
        skipped_risk_ratio = 0
        skipped_no_filler_candidates = 0
        skipped_no_fillers = 0
        skipped_float_fail = 0
        skipped_zero_input_cost = 0
        skipped_over_budget = 0
        skipped_over_max_investment = 0
        skipped_prob_fail = 0

        # Бюджет — эвристика. Жесткий лимит часто дает 0 результатов на дорогих коллекциях.
        # Разрешаем превышение (по умолчанию до 2.5x), но сохраняем метрику utilization.
        max_budget_utilization = 10.0

        target_collections = [target_collection] if target_collection is not None else list(all_collections)

        for target_c in target_collections:
            if not target_c:
                continue

            for input_rarity in input_rarities:
                potential = self._compute_collection_potential(
                    target_c,
                    input_rarity,
                    is_stattrak,
                    wears=wears,
                )
                if not potential or float(potential.get('output_multiplier') or 0.0) <= float(self._output_multiplier_threshold):
                    skipped_potential_fail += 1
                    _push_diag_sample({
                        'reason': 'potential_fail',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                        'output_multiplier': float(potential.get('output_multiplier') or 0.0) if isinstance(potential, dict) else None,
                        'threshold': float(self._output_multiplier_threshold),
                    })
                    continue

                outputs = self._get_possible_outputs(
                    target_c,
                    input_rarity,
                    average_float=None,
                    is_stattrak=is_stattrak,
                )
                if not outputs:
                    skipped_no_outputs += 1
                    continue

                best_out_price = 0.0
                best_out_name = None
                best_out_wear = None
                for wear in wears:
                    for out in outputs:
                        out_price = self._cached_get_price(
                            out['name'],
                            target_wear=wear,
                            exclude_stattrak=not is_stattrak,
                            require_stattrak=bool(is_stattrak),
                            strict_name_match=False,
                            allow_refresh=False,
                        )
                        if out_price and float(out_price) > best_out_price:
                            best_out_price = float(out_price)
                            best_out_name = out['name']
                            best_out_wear = wear

                if not best_out_price or not best_out_name:
                    skipped_no_output_prices += 1
                    _push_diag_sample({
                        'reason': 'no_output_prices',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                    })
                    continue

                budget_total = best_out_price * (core_count / 10.0)
                if budget_total <= 0:
                    skipped_budget_total += 1
                    _push_diag_sample({
                        'reason': 'budget_total',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                        'best_out_price': float(best_out_price or 0.0),
                        'core_count': int(core_count),
                        'budget_total': float(budget_total),
                    })
                    continue

                core_skins = self._get_main_skins(
                    target_c,
                    core_count,
                    is_stattrak,
                    rarity=input_rarity,
                    max_float=0.15,
                )
                if len(core_skins) < core_count:
                    core_skins = self._get_main_skins(
                        target_c,
                        core_count,
                        is_stattrak,
                        rarity=input_rarity,
            max_float=0.38,
                    )
                if len(core_skins) < core_count:
                    skipped_no_core += 1
                    _push_diag_sample({
                        'reason': 'no_core',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                        'core_count': core_count,
                        'have_core': len(core_skins),
                    })
                    continue

                core_prices = [s.get('price') for s in core_skins if s.get('price')]
                if not core_prices:
                    skipped_no_core_prices += 1
                    _push_diag_sample({
                        'reason': 'no_core_prices',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                        'core_count': int(core_count),
                    })
                    continue

                min_core_price = min((float(p) for p in core_prices if p is not None), default=None)
                if min_core_price is None or min_core_price <= 0:
                    skipped_min_core_price += 1
                    _push_diag_sample({
                        'reason': 'min_core_price',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                        'min_core_price': None if min_core_price is None else float(min_core_price),
                    })
                    continue
                # Цена филлеров: эвристика. Слишком жесткий cap дает 0 филлеров почти всегда.
                # Делаем адаптивный cap: пытаемся от 10% и выше.
                # Также добавляем разумный минимум в долларах, иначе при очень дешевом core
                # filler_price_cap становится микроскопическим и пул филлеров пустой.
                cap_ratios = [0.10, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00, 1.25]
                min_abs_filler_cap = 0.25
                try:
                    max_ratio = float(self._filler_to_target_price_ratio)
                except Exception:
                    max_ratio = 0.75
                if max_ratio <= 0:
                    max_ratio = 0.75
                # оставляем возможность поднимать cap до 1.25x, но мягко штрафуем такие варианты по цене
                max_ratio = min(max_ratio, 1.25)
                if max_ratio not in cap_ratios:
                    cap_ratios.append(max_ratio)
                cap_ratios = sorted(set(float(x) for x in cap_ratios if float(x) > 0))

                # Step 1 (gap scan): FN price of the best output must be >= 3x of core price.
                # If FN price is not available, fall back to the best available output price.
                best_out_fn_price = self._cached_get_price(
                    best_out_name,
                    target_wear='Factory New',
                    exclude_stattrak=not is_stattrak,
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=False,
                    allow_refresh=False,
                )
                best_out_fn_price = float(best_out_fn_price) if best_out_fn_price else 0.0
                gap_price = best_out_fn_price if best_out_fn_price > 0.0 else float(best_out_price)
                if float(min_core_price) > 0.0 and (gap_price / float(min_core_price)) < 3.0:
                    skipped_gap_fail += 1
                    _push_diag_sample({
                        'reason': 'gap_fail',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                        'gap_price': float(gap_price),
                        'min_core_price': float(min_core_price),
                        'ratio': float(gap_price) / float(min_core_price) if float(min_core_price) > 0 else None,
                    })
                    continue

                candidates: List[Tuple[int, float, str]] = []
                for c in all_collections:
                    if c == target_c:
                        continue

                    p2 = self._compute_collection_potential(
                        c,
                        input_rarity,
                        is_stattrak,
                        wears=wears,
                    )
                    if not p2 or float(p2.get('output_multiplier') or 0.0) <= float(self._output_multiplier_threshold):
                        skipped_suite_potential_fail += 1
                        continue
                    outcomes_count = self._get_next_grade_skins_count(c, input_rarity, is_stattrak)
                    if outcomes_count <= 0:
                        continue

                    cheap = self._get_main_skins(
                        c,
                        count=1,
                        is_stattrak=is_stattrak,
                        rarity=input_rarity,
                        max_float=1.0,
                    )
                    cheap_price = float(cheap[0].get('price') or 1e9) if cheap else 1e9
                    candidates.append((int(outcomes_count), cheap_price, c))

                candidates.sort(key=lambda x: (x[0], x[1]))
                candidates = candidates[: max(1, int(max_filler_collections))]

                if not candidates:
                    skipped_no_filler_candidates += 1
                    _push_diag_sample({
                        'reason': 'no_filler_candidates',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                        'core_count': core_count,
                        'min_core_price': float(min_core_price),
                        'filler_price_cap': float(filler_price_cap),
                    })
                    continue

                # Важно: филлеры должны быть из ОДНОЙ коллекции, иначе размывается шанс (tickets).
                # Поэтому перебираем кандидатов-коллекций и берем филлеры только из одной.
                # Use normalized-float thresholds for candidate screening.

                made = False
                contract_skins = None
                avg_float = None
                expected_wear = None
                picked_fillers = []
                used_collections = []
                used_outcomes = []

                # First try MW threshold (0.15), then FT threshold (0.38) in avg_norm space.
                for target_avg in [0.15, 0.38]:
                    max_filler_float = min(target_avg, 1.0)

                    picked_fillers = []
                    used_collections = []
                    used_outcomes = []

                    best_fillers = None
                    best_collection = None
                    best_outcomes = None
                    best_total_price = None

                    # Филлеры должны быть из одной коллекции.
                    # Берём пул дешёвых филлеров, затем внутри пула выбираем минимальный float набор,
                    # чтобы пройти avg_float контракта.
                    for filler_outcomes_count, _, filler_collection in candidates:
                        pool_size = max(600, int(filler_count) * 80)
                        pool = []
                        used_cap_ratio = None
                        for cap_ratio in cap_ratios:
                            filler_price_cap = float(min_core_price) * float(cap_ratio)
                            if filler_price_cap < float(min_abs_filler_cap):
                                filler_price_cap = float(min_abs_filler_cap)
                            pool = self._get_fillers_from_collection(
                                filler_collection,
                                input_rarity,
                                int(pool_size),
                                is_stattrak,
                                # Не режем пул жёстко по float: дальше выберем самый низкий float.
                                target_float_threshold=None,
                                max_price=filler_price_cap,
                            )
                            if len(pool) >= int(filler_count):
                                used_cap_ratio = float(cap_ratio)
                                break

                        if len(pool) < int(filler_count):
                            _push_diag_sample({
                                'reason': 'no_fillers_in_collection',
                                'target_collection': target_c,
                                'input_rarity': input_rarity,
                                'core_count': core_count,
                                'filler_count': filler_count,
                                'filler_collection': filler_collection,
                                'pool_size': int(pool_size),
                                'pool_len': len(pool),
                                'filler_price_cap': max(float(min_core_price) * float(cap_ratios[-1]), float(min_abs_filler_cap)) if cap_ratios else None,
                            })
                            continue

                        # Пул уже отсортирован по (price, float). Берём самые дешёвые и из них выбираем по float.
                        cheap_pool = pool[: int(pool_size)]
                        cheap_pool_sorted = sorted(cheap_pool, key=lambda x: (x.get('float', 1.0), x.get('price', 1e9)))
                        cand = cheap_pool_sorted[: int(filler_count)]

                        candidate_contract = core_skins[:core_count] + cand
                        candidate_avg_norm = self._calculate_average_normalized_float(candidate_contract, is_stattrak)
                        if candidate_avg_norm > (target_avg + 1e-9):
                            continue
                        total_price = sum(float(s.get('price') or 0.0) for s in cand)
                        # Немного штрафуем более высокий cap, чтобы при равной цене
                        # предпочесть варианты с более дешевыми филлерами.
                        if used_cap_ratio is not None:
                            total_price = float(total_price) * (1.0 + (float(used_cap_ratio) * 0.01))
                        if best_total_price is None or total_price < best_total_price:
                            best_total_price = total_price
                            best_fillers = cand
                            best_collection = filler_collection
                            best_outcomes = int(filler_outcomes_count)

                    if best_fillers is not None:
                        picked_fillers = best_fillers
                        used_collections = [best_collection] if best_collection else []
                        used_outcomes = [best_outcomes] if best_outcomes is not None else []

                    # Fallback: если ни одна коллекция не дала достаточно филлеров,
                    # используем глобальные умные филлеры по всем коллекциям.
                    if len(picked_fillers) < int(filler_count):
                        picked_fillers = []
                        for cap_ratio in cap_ratios:
                            filler_price_cap = float(min_core_price) * float(cap_ratio)
                            if filler_price_cap < float(min_abs_filler_cap):
                                filler_price_cap = float(min_abs_filler_cap)
                            picked_fillers = self._get_smart_fillers(
                                input_rarity,
                                int(filler_count),
                                exclude_collection=target_c,
                                is_stattrak=is_stattrak,
                                target_float_threshold=float(max_filler_float),
                                max_price=filler_price_cap,
                            )
                            if len(picked_fillers) >= int(filler_count):
                                break
                        used_collections = []
                        used_outcomes = []

                    if len(picked_fillers) < int(filler_count):
                        skipped_no_fillers += 1
                        _push_diag_sample({
                            'reason': 'no_fillers',
                            'target_collection': target_c,
                            'input_rarity': input_rarity,
                            'core_count': core_count,
                            'filler_count': filler_count,
                            'min_core_price': float(min_core_price),
                            'filler_price_cap': float(filler_price_cap),
                            'picked_fillers': len(picked_fillers),
                            'used_filler_collection': used_collections[0] if used_collections else None,
                        })
                        continue

                    contract_skins = core_skins[:core_count] + picked_fillers[:filler_count]

                    if self._is_golden_filler_present(
                        picked_fillers[:filler_count],
                        input_rarity=input_rarity,
                        is_stattrak=is_stattrak,
                        target_collection=target_c,
                    ):
                        skipped_golden_filler += 1
                        _push_diag_sample({
                            'reason': 'golden_filler',
                            'target_collection': target_c,
                            'input_rarity': input_rarity,
                            'filler_collection': used_collections[0] if used_collections else None,
                        })
                        continue

                    if best_out_price and float(best_out_price) > 0.0:
                        if any(float(s.get('price') or 0.0) > float(best_out_price) for s in contract_skins):
                            skipped_best_out_ceiling += 1
                            _push_diag_sample({
                                'reason': 'best_out_ceiling',
                                'target_collection': target_c,
                                'input_rarity': input_rarity,
                                'best_out_price': float(best_out_price),
                            })
                            continue

# Determine expected wear from target skin's out_float
                    target_skin = self.database.get_skin_by_name(str(best_out_name or ''))
                    if target_skin:
                        _f_prime = self._calculate_average_normalized_float(contract_skins)
                        _out_f = float(_f_prime) * (float(target_skin.max_float) - float(target_skin.min_float)) + float(target_skin.min_float)
                        expected_wear = self._determine_wear_from_float(_out_f)
                    else:
                        expected_wear = self._determine_wear_from_float(self._calculate_average_normalized_float(contract_skins))
                else:
                    if contract_skins is None:
                        skipped_float_fail += 1
                    else:
                        skipped_float_fail += 1
                    _push_diag_sample({
                        'reason': 'float_fail',
                        'target_collection': target_c,
                        'input_rarity': input_rarity,
                        'core_count': core_count,
                        'filler_count': filler_count,
                        'picked_fillers': len(picked_fillers) if picked_fillers is not None else None,
                    })
                    continue

                    # contract_skins assembled with avg_norm_float within target threshold

                actual_p = self._calculate_output_probability(
                    contract_skins,
                    target_c,
                    is_stattrak=is_stattrak,
                )
                if actual_p + 1e-12 < float(p):
                    skipped_prob_fail += 1
                    continue

                input_cost = sum(float(s.get('price') or 0.0) for s in contract_skins)
                if input_cost <= 0:
                    skipped_zero_input_cost += 1
                    continue

                budget_utilization = (float(input_cost) / float(budget_total)) if budget_total > 0 else 0.0
                if budget_total > 0 and budget_utilization > (max_budget_utilization + 1e-9):
                    skipped_over_budget += 1
                    continue
                if max_investment and input_cost > max_investment:
                    skipped_over_max_investment += 1
                    continue

                fp_actual = self._calculate_average_normalized_float(contract_skins, is_stattrak)
                contract_data = self._calculate_contract_profit(contract_skins, target_c, is_stattrak)

                risk_metrics = self._compute_risk_metrics(contract_skins, is_stattrak=is_stattrak)
                risk_ratio = float(risk_metrics.get('risk_ratio') or 0.0)
                worst_case_loss_pct = float(risk_metrics.get('worst_case_loss_pct') or 0.0)
                if worst_case_loss_pct > float(self._max_worst_case_loss_pct):
                    continue
                if risk_ratio > float(self._max_risk_ratio):
                    roi = float(contract_data.get('roi') or 0.0)
                    pp = float(risk_metrics.get('profit_probability') or 0.0)
                    if not (roi >= float(self._risk_ratio_override_min_roi) and pp >= float(self._risk_ratio_override_min_profit_probability)):
                        continue
                contract_data.update({
                    'target_collection': target_c,
                    'is_stattrak': is_stattrak,
                    'input_skins': contract_skins,
                    'main_skins_count': core_count,
                    'filler_skins_count': filler_count,
                    'hunt_edge': (best_out_price * (1.0 - float(self.market_fee))) / input_cost if input_cost > 0 else 0.0,
                    'hunt_output': best_out_name,
                    'hunt_output_price': best_out_price,
                    'hunt_target_wear': best_out_wear,
                    'hunt_expected_wear': expected_wear,
                    'hunt_input_rarity': self._normalize_rarity(input_rarity),
                    'hunt_filler_collection': "+".join(used_collections) if used_collections else None,
                    'hunt_filler_outcomes': int(min(used_outcomes)) if used_outcomes else None,
                    'hunt_budget_total': float(budget_total),
                    'hunt_budget_utilization': float(budget_utilization),
                    'hunt_cap': float(avg_float),
                    'hunt_target_fprime': float(fp_actual),
                    'risk_ratio': float(risk_metrics.get('risk_ratio') or 0.0),
                    'fail_probability': float(risk_metrics.get('fail_probability') or 0.0),
                    'avg_fail_value_after_fee': float(risk_metrics.get('avg_fail_value_after_fee') or 0.0),
                    'min_outcome_after_fee': float(risk_metrics.get('min_outcome_after_fee') or 0.0),
                    'worst_case_loss_pct': float(risk_metrics.get('worst_case_loss_pct') or 0.0),
                    'expected_loss_on_fail': float(risk_metrics.get('expected_loss_on_fail') or 0.0),
                })
                results.append(contract_data)
                made = True

        # Цель режима: при адекватном шансе получить максимум профита с минимальными инвестициями
        # (чем меньше исходов у свиты, тем меньше размывается шанс цели)
        worthy = [
            r for r in results
            if float(r.get('net_profit') or 0.0) > 0.0 and float(r.get('roi') or 0.0) > 15.0
        ]
        if worthy:
            results = worthy

        results.sort(
            key=lambda x: (
                float(x.get('net_profit') or -1e9),
                float(x.get('roi') or -1e9),
                -float(x.get('input_cost') or 0.0),
                float(x.get('output_probability') or 0.0),
                -int(x.get('hunt_filler_outcomes') or 10**9),
                -float(x.get('hunt_edge') or 0.0),
                -float(x.get('hunt_budget_utilization') or 0.0),
            ),
            reverse=True,
        )
        if not results:
            try:
                self._logger.info(
                    "Target+Suite: 0 results for target=%s (p=%.2f core=%s fillers=%s %s). Skip reasons: no_outputs=%s no_output_prices=%s no_core=%s no_filler_candidates=%s no_fillers=%s float_fail=%s prob_fail=%s zero_input_cost=%s over_budget=%s over_max_investment=%s",
                    (target_collection or 'ALL'),
                    p,
                    core_count,
                    filler_count,
                    'ST' if is_stattrak else 'NON-ST',
                    skipped_no_outputs,
                    skipped_no_output_prices,
                    skipped_no_core,
                    skipped_no_filler_candidates,
                    skipped_no_fillers,
                    skipped_float_fail,
                    skipped_prob_fail,
                    skipped_zero_input_cost,
                    skipped_over_budget,
                    skipped_over_max_investment,
                )
            except Exception:
                pass

        self._last_target_suite_diagnostics = {
            'target': target_collection or 'ALL',
            'is_stattrak': bool(is_stattrak),
            'p': float(p),
            'core_count': int(core_count),
            'filler_count': int(filler_count),
            'skip_reasons': {
                'no_outputs': int(skipped_no_outputs),
                'no_output_prices': int(skipped_no_output_prices),
                'no_core': int(skipped_no_core),
                'potential_fail': int(skipped_potential_fail),
                'budget_total': int(skipped_budget_total),
                'no_core_prices': int(skipped_no_core_prices),
                'min_core_price': int(skipped_min_core_price),
                'gap_fail': int(skipped_gap_fail),
                'no_filler_candidates': int(skipped_no_filler_candidates),
                'no_fillers': int(skipped_no_fillers),
                'float_fail': int(skipped_float_fail),
                'prob_fail': int(skipped_prob_fail),
                'golden_filler': int(skipped_golden_filler),
                'best_out_ceiling': int(skipped_best_out_ceiling),
                'suite_potential_fail': int(skipped_suite_potential_fail),
                'avg_float_check': int(skipped_avg_float_check),
                'worst_case_loss': int(skipped_worst_case_loss),
                'risk_ratio': int(skipped_risk_ratio),
                'zero_input_cost': int(skipped_zero_input_cost),
                'over_budget': int(skipped_over_budget),
                'over_max_investment': int(skipped_over_max_investment),
            },
            'samples': list(diag_samples),
        }
        if int(max_results) > 0:
            return results[: int(max_results)]
        return results
    
    def _generate_contracts_by_type(self, collections: List[str], is_stattrak: bool, 
                                 max_investment: float = None) -> List[Dict]:
        """Генерация контрактов для указанного типа скинов"""
        contracts: List[Dict] = []
        mode = 'ST' if is_stattrak else 'NON-ST'

        total_targets = len(collections)
        for input_rarity in ["Mil-Spec", "Restricted", "Classified"]:
            diag_no_main = 0
            diag_no_fillers = 0
            diag_float_fail = 0
            diag_ok = 0

            for idx, target_collection in enumerate(collections, start=1):
                if idx == 1 or (idx % 40) == 0 or idx == total_targets:
                    self._logger.info(
                        "Cross-contracts progress (%s/%s, %s): %s/%s (current=%s)",
                        mode,
                        input_rarity,
                        'ST' if is_stattrak else 'NON-ST',
                        idx,
                        total_targets,
                        target_collection,
                    )

                for main_count in [1, 2, 3]:
                    filler_count = 10 - main_count

                    main_skins = self._get_main_skins(
                        target_collection,
                        main_count,
                        is_stattrak,
                        rarity=input_rarity,
                        max_float=0.15,
                    )
                    if len(main_skins) < main_count:
                        diag_no_main += 1
                        continue

                    main_prices = [s.get('price') for s in main_skins if s.get('price')]
                    avg_main_price = (sum(main_prices) / len(main_prices)) if main_prices else None
                    # Филлеры должны быть заметно дешевле основы.
                    # Делаем кап мягче, иначе часто невозможно набрать 7-9 штук.
                    # Филлеры всё равно сортируются по цене и выбираются самыми дешёвыми.
                    filler_price_cap = None

                    float_targets = [0.15]
                    fillers: List[Dict] = []
                    for target_avg in float_targets:
                        required_filler_threshold = min(target_avg, 1.0)

                        # Филлеры строго из одной коллекции, иначе размывается шанс.
                        filler_candidates: List[Tuple[int, float, str]] = []
                        for filler_collection in collections:
                            if filler_collection == target_collection:
                                continue
                            outcomes_count = self._get_next_grade_skins_count(
                                filler_collection,
                                input_rarity,
                                is_stattrak,
                            )
                            if outcomes_count <= 0:
                                continue
                            cheap = self._get_main_skins(
                                filler_collection,
                                count=1,
                                is_stattrak=is_stattrak,
                                rarity=input_rarity,
                                max_float=1.0,
                            )
                            cheap_price = float(cheap[0].get('price') or 1e9) if cheap else 1e9
                            filler_candidates.append((int(outcomes_count), cheap_price, filler_collection))

                        filler_candidates.sort(key=lambda x: (x[0], x[1]))
                        filler_candidates = filler_candidates[:40]

                        fillers = []
                        best_fillers = None
                        best_total_price = None

                        for _, _, filler_collection in filler_candidates:
                            pool_size = max(200, int(filler_count) * 30)
                            pool = self._get_fillers_from_collection(
                                filler_collection,
                                input_rarity,
                                int(pool_size),
                                is_stattrak,
                                target_float_threshold=None,
                                max_price=filler_price_cap,
                            )
                            if len(pool) < int(filler_count):
                                continue

                            cheap_pool = pool[: int(pool_size)]
                            cheap_pool_sorted = sorted(cheap_pool, key=lambda x: (x.get('float', 1.0), x.get('price', 1e9)))
                            cand = cheap_pool_sorted[: int(filler_count)]
                            candidate_contract = main_skins + cand
                            candidate_avg_norm = self._calculate_average_normalized_float(candidate_contract, is_stattrak)
                            if candidate_avg_norm > (target_avg + 1e-9):
                                continue
                            total_price = sum(float(s.get('price') or 0.0) for s in cand)
                            if best_total_price is None or total_price < best_total_price:
                                best_total_price = total_price
                                best_fillers = cand

                        if best_fillers is not None:
                            fillers = best_fillers

                    # Fallback: если не получилось набрать филлеры из одной коллекции,
                    # пробуем "умные" филлеры по всем коллекциям (шанс будет размываться,
                    # но это лучше, чем 0 результатов при неполном кэше цен).
                    if len(fillers) < filler_count:
                        fillers = self._get_smart_fillers(
                            input_rarity,
                            filler_count,
                            exclude_collection=target_collection,
                            is_stattrak=is_stattrak,
                            target_float_threshold=float(required_filler_threshold or 0.20),
                            max_price=filler_price_cap,
                        )

                    if len(fillers) < filler_count:
                        diag_no_fillers += 1
                        continue

                    contract_skins = main_skins + fillers[:filler_count]
                    if self._calculate_average_normalized_float(contract_skins, is_stattrak) > 0.15:
                        diag_float_fail += 1
                        continue

                    contract_data = self._calculate_contract_profit(
                        contract_skins, target_collection, is_stattrak
                    )

                    if max_investment and contract_data['input_cost'] > max_investment:
                        continue

                    contract_data.update({
                        'target_collection': target_collection,
                        'is_stattrak': is_stattrak,
                        'input_skins': contract_skins,
                        'main_skins_count': main_count,
                        'filler_skins_count': filler_count
                    })
                    contracts.append(contract_data)
                    diag_ok += 1

            self._logger.info(
                "Cross-contracts diag (%s/%s): no_main=%d no_fillers=%d float_fail=%d ok=%d",
                mode, input_rarity, diag_no_main, diag_no_fillers, diag_float_fail, diag_ok,
            )

        return contracts

    def _get_main_skins(self, collection: str, count: int, is_stattrak: bool, rarity: Optional[str] = None,
                        max_float: float = 1.0) -> List[Dict]:
        """Получение самых дешевых основных скинов из коллекции"""
        rarity_norm = self._normalize_rarity(rarity) if rarity else None
        mf = round(float(max_float), 4) if max_float is not None else None
        memo_key = (collection, int(count), bool(is_stattrak), rarity_norm, mf)
        with self._lock_main_skins:
            cached = self._memo_main_skins.get(memo_key)
        if cached is not None:
            return list(cached)

        collection_skins = self.database.get_collection_skins(collection)

        if rarity:
            rarity = rarity_norm
            collection_skins = [s for s in collection_skins if self._normalize_rarity(s.rarity) == rarity]

        # Получаем цены и сортируем
        priced_skins = []
        for skin in collection_skins:
            price_info = self._get_skin_price_info(
                skin.name,
                max_float=max_float,
                exclude_stattrak=not is_stattrak,
                require_stattrak=bool(is_stattrak),
                strict_name_match=True,
                allow_refresh=False,
            )
            if not price_info:
                continue
            if not self._filter_skin_by_float(price_info, max_float=max_float):
                continue
            price, skin_float, wear = price_info
            if price and price > 0:
                priced_skins.append(
                    self._build_skin_entry(
                        skin.name, collection, price_info,
                        self._normalize_rarity(skin.rarity),
                    )
                )

        priced_skins.sort(key=lambda x: x['price'])
        result = priced_skins[:count]
        with self._lock_main_skins:
            self._memo_main_skins[memo_key] = list(result)
        return result

    def _get_fillers_from_collection(
        self,
        filler_collection: str,
        rarity: str,
        count: int,
        is_stattrak: bool,
        *,
        target_float_threshold: Optional[float] = 1.0,
        max_price: Optional[float] = None,
        allow_fallback_unrestricted_float: bool = True,
    ) -> List[Dict]:
        collection_skins = self.database.get_collection_skins(filler_collection)
        rarity = self._normalize_rarity(rarity)

        def _collect(max_float_threshold: Optional[float]) -> List[Dict]:
            candidate_skins: List[Dict] = []

            for skin in collection_skins:
                if self._normalize_rarity(skin.rarity) != rarity:
                    continue

                price_info = self._get_skin_price_info(
                    skin.name,
                    max_float=max_float_threshold if (max_float_threshold is not None and float(max_float_threshold) < 0.999) else None,
                    exclude_stattrak=not is_stattrak,
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=True,
                    allow_refresh=False,
                )
                if not price_info:
                    continue
                if not self._filter_skin_by_float(price_info, max_float=max_float_threshold):
                    continue

                price, skin_float, wear = price_info
                if max_price is not None and price is not None and float(price) > float(max_price):
                    continue

                outcomes_count = self._get_next_grade_skins_count(filler_collection, rarity, is_stattrak)
                if outcomes_count <= 0:
                    continue

                candidate_skins.append(
                    self._build_skin_entry(
                        skin.name, filler_collection, price_info,
                        self._normalize_rarity(skin.rarity),
                        outcomes_count=outcomes_count,
                        efficiency=0.0,
                    )
                )

            # Филлеры должны быть дешёвыми, а float низким. Приоритет — цена, затем float.
            candidate_skins.sort(key=lambda x: (x.get('price', 1e9), x.get('float', 1.0)))
            return candidate_skins

        candidate_skins = _collect(target_float_threshold)
        if (
            target_float_threshold is not None
            and len(candidate_skins) < int(count)
            and allow_fallback_unrestricted_float
            and float(target_float_threshold) < 0.999
        ):
            candidate_skins = _collect(1.0)
        return candidate_skins[: int(count)]

    def _get_next_rarity(self, input_rarity: str) -> Optional[str]:
        input_rarity = self._normalize_rarity(input_rarity)
        rarity_map = {
            'Consumer': 'Industrial',
            'Industrial': 'Mil-Spec',
            'Mil-Spec': 'Restricted',
            'Restricted': 'Classified',
            'Classified': 'Covert'
        }
        return rarity_map.get(input_rarity)
    
    def _get_smart_fillers(self, rarity: str, count: int, exclude_collection: str,
                         is_stattrak: bool, target_float_threshold: float = 0.20,
                         max_price: Optional[float] = None) -> List[Dict]:
        """Получение умных филлеров с учетом float"""
        all_skins = list(self.database.skins.values())

        rarity = self._normalize_rarity(rarity)
        
        # Фильтруем по редкости и типу StatTrak
        candidate_skins = []
        for skin in all_skins:
            if self._normalize_rarity(skin.rarity) != rarity:
                continue
            
            if skin.collection == exclude_collection:
                continue
            
            # Получаем цену и float через единый метод
            price_info = self._get_skin_price_info(
                skin.name,
                max_float=None,
                exclude_stattrak=not is_stattrak,
                require_stattrak=bool(is_stattrak),
                strict_name_match=False,
                allow_refresh=False,
            )

            if price_info:
                price, skin_float, wear = price_info
                if max_price is not None and price is not None and price > max_price:
                    continue
                if skin_float is not None and skin_float < target_float_threshold:
                    outcomes_count = self._get_next_grade_skins_count(skin.collection, rarity, is_stattrak)
                    if outcomes_count <= 0:
                        continue
                    candidate_skins.append(
                        self._build_skin_entry(
                            skin.name, skin.collection, price_info,
                            self._normalize_rarity(skin.rarity),
                            outcomes_count=outcomes_count,
                            efficiency=0.0,
                        )
                    )
        
        # Сортируем:
        # 1) минимум outcomes (меньше "размывает" шанс)
        # 2) максимально низкий float (компенсация плохого float у основных скинов)
        # 3) максимальная эффективность
        # 4) цена
        candidate_skins.sort(
            key=lambda x: (
                x.get('outcomes_count', 999999),
                x.get('float', 1.0),
                x.get('price', 1e9),
            )
        )
        return candidate_skins[:count]
    
    def _calculate_output_probability(self, contract_skins: List[Dict], target_collection: str, is_stattrak: bool) -> float:
        """Вероятность, что результат будет из target_collection (0..1)."""
        total_skins = len(contract_skins)
        if total_skins <= 0:
            return 0.0

        input_rarity = contract_skins[0].get('rarity') if contract_skins else None
        if not input_rarity:
            return 0.0

        collections_count = defaultdict(int)
        for skin in contract_skins:
            collections_count[skin['collection']] += 1

        # tickets: n_inputs_in_collection * outcomes_in_collection
        total_tickets = 0
        tickets_target = 0
        for collection_name, skins_count in collections_count.items():
            outcomes_count = self._get_next_grade_skins_count(collection_name, input_rarity, is_stattrak=is_stattrak)
            tickets = skins_count * outcomes_count
            total_tickets += tickets
            if collection_name == target_collection:
                tickets_target = tickets

        if total_tickets <= 0:
            return 0.0

        return tickets_target / total_tickets
    
    def _get_next_grade_skins_count(self, collection: str, input_rarity: str, is_stattrak: bool) -> int:
        """Количество скинов следующего грейда для данного входного грейда в коллекции."""
        input_rarity = self._normalize_rarity(input_rarity)
        next_rarity = self._get_next_rarity(input_rarity)
        if not next_rarity:
            return 0

        memo_key = (collection, input_rarity, next_rarity, bool(is_stattrak))
        with self._lock_next_grade:
            cached = self._memo_next_grade_count.get(memo_key)
        if cached is not None:
            return int(cached)

        collection_skins = self.database.get_collection_skins(collection)
        count = 0
        for skin in collection_skins:
            if self._normalize_rarity(skin.rarity) != next_rarity:
                continue
            count += 1

        with self._lock_next_grade:
            self._memo_next_grade_count[memo_key] = int(count)
        return int(count)
    
    def _calculate_average_float(self, contract_skins: List[Dict]) -> float:
        """Расчет среднего флоута контракта"""
        if not contract_skins:
            return 0.0
        
        total_float = 0
        valid_skins = 0
        
        for skin in contract_skins:
            skin_float = skin.get('float', None)
            if skin_float is None:
                estimated = self._estimate_float_from_wear(skin.get('wear'))
                if estimated is not None:
                    skin_float = estimated
                else:
                    continue
            total_float += float(skin_float)
            valid_skins += 1
        
        return total_float / valid_skins if valid_skins > 0 else 0.0

    def _calculate_average_normalized_float(self, contract_skins: List[Dict]) -> float:
        """Средний нормализованный float входа (0..1) с учетом min/max диапазона каждого скина."""
        if not contract_skins:
            return 0.0

        total = 0.0
        valid = 0
        for skin in contract_skins:
            skin_float = skin.get('float', None)
            if skin_float is None:
                estimated = self._estimate_float_from_wear(skin.get('wear'))
                if estimated is not None:
                    skin_float = estimated
                else:
                    continue

            skin_data = self.database.get_skin_by_name(skin.get('name', ''))
            if not skin_data:
                continue

            try:
                min_f = float(skin_data.min_float)
                max_f = float(skin_data.max_float)
            except Exception:
                continue

            denom = max_f - min_f
            if denom <= 1e-9:
                continue

            norm = (float(skin_float) - min_f) / denom
            if norm < 0.0:
                norm = 0.0
            if norm > 1.0:
                norm = 1.0

            total += norm
            valid += 1

        return total / valid if valid > 0 else 0.0

    def _calculate_skinsearch_normalized_float(
        self,
        contract_skins: List[Dict],
        output_min_float: float,
        output_max_float: float
    ) -> float:
        """
        Рассчитывает нормализованный float согласно алгоритму skinsearch/CS2.
        
        Формула: f' = (avg_float - min_f_output) / (max_f_output - min_f_output)
        
        Где avg_float — среднее абсолютных float входных скинов,
        а min_f_output/max_f_output — диапазон ВЫХОДНОГО скина.
        
        Edge cases:
        - max_f_output - min_f_output <= 0 → 0.0 (скин с фиксированным float)
        - avg_float < min_f_output → clamped до 0.0
        - avg_float > max_f_output → clamped до 1.0
        """
        if not contract_skins:
            return 0.0
        
        # Расчет среднего абсолютного float
        avg_float = self._calculate_average_float(contract_skins)
        
        # Edge case: скин с фиксированным float
        output_range = output_max_float - output_min_float
        if output_range <= 1e-9:
            return 0.0
        
        # Нормализация относительно выходного скина
        norm = (avg_float - output_min_float) / output_range
        
        # Clamping
        if norm < 0.0:
            norm = 0.0
        if norm > 1.0:
            norm = 1.0
        
        return norm

    def _calculate_weighted_average_normalized_float(self, contract_skins: List[Dict], is_stattrak: bool) -> float:
        """
        Рассчитывает weighted average normalized float согласно алгоритму skinsearch.
        
        Для каждого входного скина:
        1. Находим все возможные выходные скины из его коллекции
        2. Определяем min/max float среди этих выходных скинов
        3. Нормализуем float входного скина относительно этого диапазона
        4. Взвешиваем по количеству возможных исходов из этой коллекции
        """
        if not contract_skins:
            return 0.0

        input_rarity = contract_skins[0].get('rarity') if contract_skins else None
        if not input_rarity:
            return 0.0

        weighted_sum = 0.0
        total_weight = 0.0

        for skin in contract_skins:
            skin_float = skin.get('float', None)
            if skin_float is None:
                estimated = self._estimate_float_from_wear(skin.get('wear'))
                if estimated is not None:
                    skin_float = estimated
                else:
                    continue

            collection = skin.get('collection', '')
            if not collection:
                continue

            # Получаем количество возможных исходов из этой коллекции
            outcomes_count = self._get_next_grade_skins_count(
                collection,
                input_rarity,
                is_stattrak=is_stattrak,
            )
            if outcomes_count <= 0:
                continue

            # Нормализуем float входного скина относительно его собственного диапазона
            skin_name = skin.get('name') or ''
            skin_data = self.database.get_skin_by_name(str(skin_name))
            if skin_data:
                smin = float(skin_data.min_float)
                smax = float(skin_data.max_float)
                sdenom = smax - smin
                if sdenom > 1e-9:
                    norm = (float(skin_float) - smin) / sdenom
                else:
                    norm = 0.0
            else:
                norm = 0.5
            norm = max(0.0, min(1.0, norm))

            weighted_sum += norm * float(outcomes_count)
            total_weight += float(outcomes_count)

        if total_weight <= 0:
            logger.warning(f"_calculate_weighted_average_normalized_float: all {len(contract_skins)} skins have no valid float data")
            return 0.0
        return weighted_sum / total_weight
    
    def _determine_best_achievable_wear(self, average_float: float) -> str:
        """Определение лучшего достижимого качества"""
        # Align with external calculators: wear is determined by the normalized average float (f')
        # Use the same thresholds as _determine_wear_from_float for consistency
        f = float(average_float)
        
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

    def _determine_wear_from_float(
        self, 
        item_float: float, 
        available_wears: Optional[List[str]] = None
    ) -> str:
        """
        Определяет качество (wear) по значению float с учётом доступных wear для скина.

        CS2 wear ranges (inclusive upper bound):
        FN: [0.00, 0.07], MW: (0.07, 0.15], FT: (0.15, 0.38], WW: (0.38, 0.45], BS: (0.45, 1.00]

        Args:
            item_float: Float value (0.0-1.0)
            available_wears: List of available wear levels for this skin.
                            If None, uses standard thresholds.

        Returns:
            Wear level string
        """
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

    def calculate_contract_outcomes_details(self, contract_skins: List[Dict], is_stattrak: bool) -> List[Dict]:
        # skinsearch algorithm: f' = average of per-skin normalized floats (own range),
        # then out_float = f' * (max_f - min_f) + min_f, wear from out_float.
        avg_norm = self._calculate_average_normalized_float(contract_skins)
        if avg_norm < 0.0:
            avg_norm = 0.0
        if avg_norm > 1.0:
            avg_norm = 1.0
        
        self._logger.debug('ContractCalc: avg_norm=%.4f inputs=%d', avg_norm, len(contract_skins))
        for i, s in enumerate(contract_skins[:3]):
            self._logger.debug('  input[%d]: name=%s float=%s wear=%s', i, s.get('name'), s.get('float'), s.get('wear'))
        
        input_rarity = contract_skins[0].get('rarity') if contract_skins else None
        if not input_rarity:
            return []

        collections_count = defaultdict(int)
        for skin in contract_skins:
            collections_count[skin['collection']] += 1

        output_items_by_collection: Dict[str, List[Dict]] = {}
        for collection_name, skins_count in collections_count.items():
            output_items = self._get_possible_outputs(collection_name, input_rarity, average_float=None, is_stattrak=is_stattrak)
            if not output_items:
                continue
            output_items_by_collection[collection_name] = output_items

        if not output_items_by_collection:
            return []

        outcomes: List[Dict] = []
        for collection_name, output_items in output_items_by_collection.items():
            skins_count = collections_count[collection_name]
            collection_prob = skins_count / 10.0
            per_item_prob = collection_prob / float(len(output_items))

            for item in output_items:
                skin_name = item['name']
                skin_data = self.database.get_skin_by_name(skin_name)
                if skin_data:
                    try:
                        min_f = float(skin_data.min_float)
                        max_f = float(skin_data.max_float)
                    except Exception:
                        min_f, max_f = 0.0, 1.0
                else:
                    min_f, max_f = 0.0, 1.0

                try:
                    raw_wears = list(getattr(skin_data, 'wears', None) or []) if skin_data else []
                    # Normalize wear entries: they may be strings or dicts with a 'name' field.
                    wears_avail = [w.get('name') if isinstance(w, dict) and 'name' in w else str(w) for w in raw_wears]
                except Exception:
                    wears_avail = []

                if min_f < 0.0:
                    min_f = 0.0
                if max_f > 1.0:
                    max_f = 1.0
                if max_f <= min_f + 1e-9:
                    min_f, max_f = 0.0, 1.0

                out_float = float(avg_norm) * (max_f - min_f) + min_f
                
                available_wears_for_skin = wears_avail if wears_avail else None
                wear = self._determine_wear_from_float(out_float, available_wears=available_wears_for_skin)
                
                if 'Buzz Kill' in skin_name or 'Dragonfire' in skin_name:
                    self._logger.info(
                        'WEAR_FIX_DEBUG: skin=%s avg_norm=%.4f out_float=%.4f wears=%s -> wear=%s',
                        skin_name, avg_norm, out_float, available_wears_for_skin, wear
                    )
                
                if available_wears_for_skin:
                    ideal_wear = self._determine_wear_from_float(out_float, available_wears=None)
                    if ideal_wear != wear:
                        self._logger.info(
                            'WearDegradation: skin=%s ideal=%s actual=%s float=%.4f available=%s',
                            skin_name, ideal_wear, wear, out_float, available_wears_for_skin
                        )

                # Liquidity-aware pricing: use effective sell price to avoid paper-profit on illiquid skins.
                price = self._cached_get_effective_sell_price(
                    skin_name,
                    target_wear=wear,
                    max_float=None,
                    exclude_stattrak=not is_stattrak,
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=False,
                    allow_refresh=False,
                )
                price = float(price) if price is not None else 0.0

                sell_source = 'MARKETCSGO'
                sell_fee = float(self.market_fee)
                if bool(self._multisource_net_pricing):
                    market_net = float(price) * (1.0 - float(self.market_fee))
                    best_net = float(market_net)

                    csfloat_net = None
                    try:
                        cfc = getattr(self.price_manager, 'csfloat_client', None)
                        if cfc is not None and bool(getattr(cfc, 'enabled', False)):
                            cs_price = cfc.get_price(
                                skin_name,
                                target_wear=wear,
                                max_float=float(out_float),
                                exclude_stattrak=not is_stattrak,
                                require_stattrak=bool(is_stattrak),
                                limit=50,
                            )
                            if cs_price is not None and float(cs_price) > 0:
                                csfloat_net = float(cs_price) * (1.0 - float(self.csfloat_fee))
                    except Exception:
                        csfloat_net = None

                    if csfloat_net is not None and float(csfloat_net) > float(best_net) + 1e-12:
                        best_net = float(csfloat_net)
                        sell_source = 'CSFLOAT'
                        sell_fee = float(self.csfloat_fee)

                    price = float(best_net)

                outcomes.append({
                    'name': skin_name,
                    'collection': collection_name,
                    'probability': per_item_prob,
                    'out_float': out_float,
                    'min_float': float(min_f),
                    'max_float': float(max_f),
                    'wear': wear,
                    'price': price,
                    'sell_source': sell_source,
                    'sell_fee': float(sell_fee),
                })

        return outcomes
    
    def _calculate_contract_profit(self, contract_skins: List[Dict], target_collection: str, 
                                is_stattrak: bool) -> Dict:
        """Расчет профита контракта"""
        # Стоимость входных скинов
        input_cost = sum(skin['price'] for skin in contract_skins)

        # Вероятность, что результат будет из целевой коллекции (система билетов)
        output_probability = self._calculate_output_probability(
            contract_skins,
            target_collection,
            is_stattrak=is_stattrak,
        )

        # Расчет среднего флоута входа
        avg_float = self._calculate_average_float(contract_skins)
        avg_norm_float = self._calculate_average_normalized_float(contract_skins)

        # Считаем все возможные исходы с индивидуальным out_float/wear/price
        outcomes = self.calculate_contract_outcomes_details(contract_skins, is_stattrak=is_stattrak)

        best_outcome_name = None
        best_outcome_price = 0.0
        best_outcome_probability = 0.0

        gross_ev = 0.0
        profit_probability = 0.0
        for o in outcomes:
            prob = float(o.get('probability') or 0.0)
            price = float(o.get('price') or 0.0)
            gross_ev += price * prob
            # PP = probability that the market value of the outcome exceeds input cost.
            # When _multisource_net_pricing=True (refine step), price is already net;
            # when False (initial eval), price is gross market value.
            # We compare as-is: PP reflects market-value profitability.
            # Selling fees are captured separately in ROI/EV.
            if price > input_cost:
                profit_probability += prob

            if price > 0.0 and price > float(best_outcome_price):
                best_outcome_name = str(o.get('name') or '')
                best_outcome_price = float(price)
                best_outcome_probability = float(prob)

        if bool(self._multisource_net_pricing):
            ev_after_fee = float(gross_ev)
        else:
            # EV с учётом комиссии рынка (market.csgo.com ~7%, не Steam 15%)
            ev_after_fee = gross_ev * (1.0 - float(self.market_fee))
        net_profit = ev_after_fee - input_cost
        roi = (net_profit / input_cost) * 100 if input_cost > 0 else 0

        # Шаг 4: Определяем achievable_wear из outcomes (worst wear)
        if outcomes:
            _wear_order = ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']
            _worst_idx = 0
            for o in outcomes:
                w = str(o.get('wear') or '')
                try:
                    idx = _wear_order.index(w)
                except ValueError:
                    idx = len(_wear_order) - 1
                if idx > _worst_idx:
                    _worst_idx = idx
            achievable_wear = _wear_order[_worst_idx]
        else:
            achievable_wear = self._determine_wear_from_float(avg_norm_float)

        return {
            'input_cost': input_cost,
            'expected_output': ev_after_fee,
            'net_profit': net_profit,
            'roi': roi,
            'output_probability': output_probability,
            'profit_probability': profit_probability,
            'average_float': avg_float,
            'average_normalized_float': avg_norm_float,
            'achievable_wear': achievable_wear,
            'best_outcome_name': best_outcome_name,
            'best_outcome_price': float(best_outcome_price) if best_outcome_price else 0.0,
            'best_outcome_probability': float(best_outcome_probability) if best_outcome_probability else 0.0,
        }
    
    _WEAR_RANGES: Dict[str, Tuple[float, float]] = {
        'Factory New': (0.0, 0.07),
        'Minimal Wear': (0.07, 0.15),
        'Field-Tested': (0.15, 0.38),
        'Well-Worn': (0.38, 0.45),
        'Battle-Scarred': (0.45, 1.0),
    }

    def _get_possible_outputs(self, collection: str, input_rarity: str, average_float: Optional[float], is_stattrak: bool) -> List[Dict]:
        """Получение возможных выходных скинов для конкретной коллекции и входного грейда.

        Если average_float задан, определяется лучшее достижимое качество через
        _determine_best_achievable_wear и скины фильтруются: остаются только те,
        чей диапазон [min_float, max_float] пересекается с диапазоном этого качества.
        Если average_float is None — возвращаются все скины следующего грейда.
        """
        input_rarity = self._normalize_rarity(input_rarity)
        next_rarity = self._get_next_rarity(input_rarity)
        if not next_rarity:
            return []

        target_wear = self._determine_best_achievable_wear(float(average_float)) if average_float is not None else None

        memo_key = (collection, input_rarity, next_rarity, bool(is_stattrak), target_wear)
        with self._lock_next_grade:
            cached = self._memo_possible_outputs.get(memo_key)
        if cached is not None:
            return list(cached)

        collection_skins = self.database.get_collection_skins(collection)

        possible_outputs = []
        for skin in collection_skins:
            if self._normalize_rarity(skin.rarity) != next_rarity:
                continue

            if target_wear is not None:
                wear_lo, wear_hi = self._WEAR_RANGES.get(target_wear, (0.0, 1.0))
                try:
                    smin = float(skin.min_float)
                    smax = float(skin.max_float)
                except Exception:
                    smin, smax = 0.0, 1.0
                if smin > wear_hi + 1e-12 or smax < wear_lo - 1e-12:
                    continue

            possible_outputs.append({
                'name': skin.name,
                'rarity': skin.rarity
            })

        with self._lock_next_grade:
            self._memo_possible_outputs[memo_key] = list(possible_outputs)
        return possible_outputs
    
    def find_cheap_fillers(self, rarity: str = None, max_float: float = None, limit: int = 20) -> Dict[str, List[Dict]]:
        """
        Поиск дешевых филлеров для контрактов
        
        Args:
            rarity: уровень редкости (None для всех)
            max_float: максимальный float (None для всех, по умолчанию None)
            limit: количество результатов
            
        Returns:
            Dict с топовыми дешевыми скинами по редкостям
        """
        # Получаем все скины из базы
        all_skins = list(self.database.skins.values())
        
        # Группируем по редкости
        rarity_groups = defaultdict(list)
        for skin in all_skins:
            rarity_groups[skin.rarity].append(skin)
        
        results = {}
        
        for rarity_name, skins in rarity_groups.items():
            if rarity and rarity_name != rarity:
                continue
            
            cheap_skins = []
            
            for skin in skins:
                # Ищем цену без строгого фильтра по float для начала
                price_info = self.price_manager.get_skin_price_with_float(
                    skin.name, 
                    max_float=max_float,  # Может быть None для всех
                    exclude_stattrak=True
                )
                
                if price_info:
                    price, item_float, wear = price_info
                    cheap_skins.append({
                        'name': skin.name,
                        'collection': skin.collection,
                        'price': price,
                        'float': item_float,
                        'wear': wear,
                        'rarity': skin.rarity
                    })
            
            # Сортируем по цене и берем топ
            cheap_skins.sort(key=lambda x: x['price'])
            results[rarity_name] = cheap_skins[:limit]
        
        return results
    
    def find_profitable_contracts(self, collection_name: str = None, max_investment: float = 50.0) -> List[ContractResult]:
        """
        Поиск прибыльных контрактов для указанной коллекции или всех коллекций
        
        Args:
            collection_name: название коллекции (None для всех)
            max_investment: максимальные инвестиции в контракт
            
        Returns:
            Список прибыльных контрактов, отсортированный по ROI
        """
        results = []
        
        if collection_name:
            # Поиск для конкретной коллекции
            collection = self.database.get_collection(collection_name)
            if not collection:
                return results
            
            # Получаем Mil-Spec скины из коллекции
            milspec_skins = self.database.get_skins_by_rarity("Mil-Spec", collection_name)
            if len(milspec_skins) < 10:
                return results
            
            results = self._find_contracts_for_collection(collection_name, milspec_skins, max_investment)
        else:
            # Поиск по всем коллекциям
            milspec_collections = self.database.get_collections_with_rarity("Mil-Spec")
            
            for coll_name in milspec_collections:
                coll_results = self.find_profitable_contracts(coll_name, max_investment)
                results.extend(coll_results)
        
        # Сортируем по ROI
        results.sort(key=lambda x: x.roi_percentage, reverse=True)
        return results
    
    def _find_contracts_for_collection(self, collection_name: str, milspec_skins: List[SkinData], max_investment: float) -> List[ContractResult]:
        """Поиск контрактов для конкретной коллекции с реальными float данными"""
        results = []
        
        # Получаем цены на Mil-Spec скины с учетом float
        milspec_names = [skin.name for skin in milspec_skins]
        milspec_prices = {}
        
        for skin_name in milspec_names:
            price_info = self.price_manager.get_skin_price_with_float(skin_name)
            if price_info:
                price, item_float, wear = price_info
                milspec_prices[skin_name] = price
        
        priced_milspec = [(name, price) for name, price in milspec_prices.items() if price and price > 0]
        
        if len(priced_milspec) < 10:
            return results
        
        # Сортируем по цене
        priced_milspec.sort(key=lambda x: x[1])
        
        # Берем разные комбинации
        for i in range(min(5, len(priced_milspec))):  # до 5 дорогих скинов
            expensive_skin = priced_milspec[i][0]
            
            # Создаем контракт: 7 самых дешевых + 3 дорогих
            filler_skins = [priced_milspec[j][0] for j in range(min(7, len(priced_milspec)))]
            contract_skins = filler_skins + [expensive_skin]
            
            # Добиваем до 10 скинов
            if len(contract_skins) < 10:
                remaining = [priced_milspec[j][0] for j in range(7, min(10, len(priced_milspec)))]
                contract_skins.extend(remaining[:10-len(contract_skins)])
            
            if len(contract_skins) == 10:
                result = self._calculate_contract_result(contract_skins, collection_name)
                if result and result.investment_cost <= max_investment and result.roi_percentage > 0:
                    results.append(result)
        
        return results
    
    def calculate_break_even_price(self, skin_name: str, probability: float) -> float:
        """
        Рассчитать минимальную цену для безубыточности
        """
        # Для безубыточности: price * probability >= investment_cost
        # investment_cost обычно ~10 * average_input_price
        # Это упрощенный расчет, в реальности нужно знать конкретные входные скины
        average_input_price = 2.0  # более реалистичная средняя цена для Mil-Spec
        investment_cost = 10 * average_input_price
        
        break_even_price = investment_cost / probability if probability > 0 else float('inf')
        return break_even_price