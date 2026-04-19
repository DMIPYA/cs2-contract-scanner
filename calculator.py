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

class ContractCalculator:

    """РљР°Р»СЊРєСѓР»СЏС‚РѕСЂ РєРѕРЅС‚СЂР°РєС‚РѕРІ CS2 СЃ СЂРµР°Р»СЊРЅС‹РјРё РґР°РЅРЅС‹РјРё"""

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
        self._memo_lock = threading.Lock()
        self._last_target_suite_diagnostics: Optional[Dict] = None
        self.rarities_hierarchy = {
            "Consumer": 0,
            "Industrial": 1, 
            "Mil-Spec": 2,
            "Restricted": 3,
            "Classified": 4,
            "Covert": 5
        }
        # Float Р·РЅР°С‡РµРЅРёСЏ РґР»СЏ РїРѕР»СѓС‡РµРЅРёСЏ Factory New
        self.float_thresholds = {
            "Factory New": 0.07,
            "Minimal Wear": 0.15,
            "Field-Tested": 0.38,
            "Well-Worn": 0.45,
            "Battle-Scarred": 1.0
        }
        # РљРѕРјРёСЃСЃРёСЏ СЂС‹РЅРєР° (market.csgo.com Р±РµСЂС‘С‚ ~7%, РЅРµ 15% РєР°Рє Steam Market)
        try:
            self.market_fee = float(os.getenv('MARKET_SELL_FEE', '0.07') or 0.07)
        except Exception:
            self.market_fee = 0.07
        try:
            self.csfloat_fee = float(os.getenv('CSFLOAT_FEE', '0.02') or 0.02)
        except Exception:
            self.csfloat_fee = 0.02
        self._multisource_net_pricing = False
        self._output_multiplier_threshold = 1.5
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
        self._memo_max_input_float: Dict[Tuple[str, str], Optional[float]] = {}  # NEW: Cache for max input float calculations

    def get_last_target_suite_diagnostics(self) -> Optional[Dict]:

        return self._last_target_suite_diagnostics

    def _wear_to_max_float(self, wear: str) -> float:

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

    def _calculate_max_input_float_for_target_wear(

        self,
        output_skin_name: str,
        target_wear: str

    ) -> Optional[float]:

        """
        Calculate maximum normalized float for input skins to guarantee target wear output.
        This function determines the maximum float value that input skins can have
        to ensure the output skin will be of the specified wear quality. It accounts
        for the skin's min/max float range and normalizes the threshold accordingly.
        Args:
            output_skin_name: Name of the output skin (e.g., "FAMAS | Rapid Eye Movement")
            target_wear: Target wear level (e.g., "Factory New", "Minimal Wear")
        Returns:
            float: Maximum normalized float [0.0, 1.0] for input skins
            None: If target wear is unachievable for this skin
        Examples:
            >>> calc._calculate_max_input_float_for_target_wear(
            ...     "FAMAS | Rapid Eye Movement",
            ...     "Factory New"
            ... )
            0.07  # Normalized threshold for FN
            >>> calc._calculate_max_input_float_for_target_wear(
            ...     "AWP | Asiimov",  # min_float=0.18
            ...     "Factory New"     # threshold=0.07
            ... )
            None  # Unachievable (min_float > threshold)
        Notes:
            - Uses wear thresholds: FN=0.07, MW=0.15, FT=0.38, WW=0.45, BS=1.00
            - Result is cached for performance
            - Thread-safe through memoization lock
        """
        # Check cache first
        cache_key = (output_skin_name, target_wear)
        with self._memo_lock:
            if cache_key in self._memo_max_input_float:
                return self._memo_max_input_float[cache_key]
        # Get skin data from database
        skin_data = self.database.get_skin_by_name(output_skin_name)
        if not skin_data:
            self._logger.warning(
                'Skin not found in database: %s',
                output_skin_name
            )
            with self._memo_lock:
                self._memo_max_input_float[cache_key] = None
            return None
        try:
            min_float = float(skin_data.min_float)
            max_float = float(skin_data.max_float)
        except (AttributeError, TypeError, ValueError) as e:
            self._logger.warning(
                'Invalid float data for skin %s: %s',
                output_skin_name,
                e
            )
            with self._memo_lock:
                self._memo_max_input_float[cache_key] = None
            return None
        # Get threshold for target wear
        WEAR_THRESHOLDS = {
            'Factory New': 0.07,
            'Minimal Wear': 0.15,
            'Field-Tested': 0.38,
            'Well-Worn': 0.45,
            'Battle-Scarred': 1.00,
        }
        max_output_float = WEAR_THRESHOLDS.get(target_wear, 1.0)
        # Check if target wear is achievable
        if min_float > max_output_float:
            # Skin cannot be this wear (e.g., AWP Asiimov cannot be Factory New)
            self._logger.debug(
                'Target wear %s unachievable for %s (min_float=%.3f > threshold=%.3f)',
                target_wear,
                output_skin_name,
                min_float,
                max_output_float
            )
            with self._memo_lock:
                self._memo_max_input_float[cache_key] = None
            return None
        # Calculate float range
        float_range = max_float - min_float
        if float_range < 1e-9:
            # Invalid or zero range
            self._logger.warning(
                'Invalid float range for %s: min=%.3f max=%.3f',
                output_skin_name,
                min_float,
                max_float
            )
            with self._memo_lock:
                self._memo_max_input_float[cache_key] = None
            return None
        # Normalize threshold to [0, 1] range relative to skin's float range
        # Formula: (target_threshold - min_float) / (max_float - min_float)
        normalized_threshold = (max_output_float - min_float) / float_range
        # Clamp to [0, 1] to handle edge cases
        normalized_threshold = max(0.0, min(1.0, normalized_threshold))
        # Cache result
        with self._memo_lock:
            self._memo_max_input_float[cache_key] = normalized_threshold
        return normalized_threshold

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
        with self._memo_lock:
            if key in self._memo_collection_avg_outcome_price:
                return self._memo_collection_avg_outcome_price[key]
        outs = self._get_possible_outputs(collection_name, input_rarity, is_stattrak=is_stattrak)
        if not outs:
            with self._memo_lock:
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
            with self._memo_lock:
                self._memo_collection_avg_outcome_price[key] = None
            return None
        val = float(sum(prices)) / float(len(prices))
        with self._memo_lock:
            self._memo_collection_avg_outcome_price[key] = float(val)
        return float(val)

    def _collection_score(self, collection_name: str, input_rarity: str, *, is_stattrak: bool) -> Optional[float]:

        input_rarity = self._normalize_rarity(input_rarity)
        key = (collection_name, input_rarity, bool(is_stattrak))
        with self._memo_lock:
            if key in self._memo_collection_score:
                return self._memo_collection_score[key]
        prices = []
        for w in ['Factory New', 'Minimal Wear', 'Field-Tested']:
            p = self._avg_outcome_price_for_collection(collection_name, input_rarity, is_stattrak=is_stattrak, target_wear=w)
            if p and float(p) > 0:
                prices.append(float(p))
        if not prices:
            with self._memo_lock:
                self._memo_collection_score[key] = None
            return None
        score = max(prices)
        with self._memo_lock:
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
            # РћС†РµРЅРёРІР°РµРј С†РµРЅСѓ РІС…РѕРґРЅС‹С… СЃРєРёРЅРѕРІ РїСЂРё РїСЂР°РІРёР»СЊРЅРѕРј РѕРіСЂР°РЅРёС‡РµРЅРёРё float
            # (С‚.Рµ. СЃСЂР°РІРЅРёРІР°РµРј FN-РІС‹С…РѕРґ/FN-РІС…РѕРґ, FT-РІС‹С…РѕРґ/FT-РІС…РѕРґ вЂ” В«СЏР±Р»РѕРєРё Рє СЏР±Р»РѕРєР°РјВ»).
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
            # Score = EV-ratio: СЃСЂРµРґРЅСЏСЏ С†РµРЅР° РІС‹С…РѕРґР° / СЃС‚РѕРёРјРѕСЃС‚СЊ 1 РІС…РѕРґРЅРѕРіРѕ СЃРєРёРЅР°
            # Р’РµСЂРѕСЏС‚РЅРѕСЃС‚СЊ РѕРґРЅР° Рё С‚Р° Р¶Рµ РґР»СЏ РІСЃРµС… wear в†’ РјРѕР¶РЅРѕ РѕРїСѓСЃС‚РёС‚СЊ.
            score = float(out_price) / float(in_price)
            if score > best_score:
                best_score = float(score)
                best_wear = w
        return best_wear

    def _find_achievable_target_wear(

        self,
        output_skin_name: str,
        preferred_wear: str,
        collection_name: str,
        input_rarity: str,
        *,
        is_stattrak: bool

    ) -> Optional[Tuple[str, float]]:

        """
        Find the first achievable wear level with automatic fallback.
        This function attempts to find a wear level that is both:
        1. Achievable for the output skin (based on its float range)
        2. Has available input skins on the market with required float
        If the preferred wear is not achievable, it automatically falls back
        to the next worse wear in the chain: FN в†’ MW в†’ FT в†’ WW в†’ BS
        Args:
            output_skin_name: Name of the output skin (e.g., "FAMAS | Rapid Eye Movement")
            preferred_wear: Preferred target wear (e.g., "Factory New")
            collection_name: Collection name for input skins
            input_rarity: Rarity of input skins (e.g., "Restricted")
            is_stattrak: Whether to search for StatTrak skins
        Returns:
            tuple: (achievable_wear, max_input_float) if found
            None: If no wear is achievable
        Examples:
            >>> calc._find_achievable_target_wear(
            ...     "AWP | Asiimov",  # min_float=0.18
            ...     "Factory New",    # Unachievable
            ...     "The Cobblestone Collection",
            ...     "Classified",
            ...     is_stattrak=False
            ... )
            ("Field-Tested", 0.244)  # Fallback to FT
        Notes:
            - Logs fallback events at INFO level
            - Checks market availability before returning
            - Returns None if no wear is achievable (rare)
        """
        # Fallback chain: try progressively worse wears
        WEAR_FALLBACK_CHAIN = [
            'Factory New',
            'Minimal Wear',
            'Field-Tested',
            'Well-Worn',
            'Battle-Scarred',
        ]
        # Find starting position in fallback chain
        try:
            start_idx = WEAR_FALLBACK_CHAIN.index(preferred_wear)
        except ValueError:
            # Invalid wear name, start from Factory New
            self._logger.warning(
                'Invalid preferred wear "%s", starting from Factory New',
                preferred_wear
            )
            start_idx = 0
        # Try each wear in the fallback chain
        for wear in WEAR_FALLBACK_CHAIN[start_idx:]:
            # Step 1: Check if this wear is achievable for the output skin
            max_float = self._calculate_max_input_float_for_target_wear(
                output_skin_name,
                wear
            )
            if max_float is None:
                # This wear is unachievable for this skin, try next
                self._logger.debug(
                    'Wear %s unachievable for %s, trying next',
                    wear,
                    output_skin_name
                )
                continue
            # Step 2: Check if input skins with required float exist on market
            # We only need to check availability, so limit=10 is enough
            try:
                candidate_inputs = self._get_candidate_inputs(
                    collection_name,
                    input_rarity,
                    is_stattrak=is_stattrak,
                    max_float=max_float,
                    limit=10,  # Just checking availability
                )
            except Exception as e:
                self._logger.warning(
                    'Failed to get candidate inputs for %s (wear=%s): %s',
                    output_skin_name,
                    wear,
                    e
                )
                continue
            if not candidate_inputs:
                # No input skins available with required float, try next wear
                self._logger.debug(
                    'No input skins available for %s with max_float=%.3f (wear=%s)',
                    output_skin_name,
                    max_float,
                    wear
                )
                continue
            # Found achievable wear with available inputs!
            if wear != preferred_wear:
                # Log fallback event С‚РѕР»СЊРєРѕ РґР»СЏ debug СЂРµР¶РёРјР°
                self._logger.debug(
                    'Fallback: %s в†’ %s for %s (ST=%s, available_inputs=%d)',
                    preferred_wear,
                    wear,
                    output_skin_name,
                    'Y' if is_stattrak else 'N',
                    len(candidate_inputs)
                )
            return (wear, max_float)
        # No achievable wear found (РѕС‡РµРЅСЊ СЂРµРґРєРѕ, РЅРѕ РЅРѕСЂРјР°Р»СЊРЅРѕ)
        # РЈР±РёСЂР°РµРј СЃРїР°Рј - РїРѕРєР°Р·С‹РІР°РµРј С‚РѕР»СЊРєРѕ РїСЂРё HUNT_DEBUG=1
        if str(os.getenv('HUNT_DEBUG', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}:
            self._logger.warning(
                'No achievable wear found for %s (preferred=%s, ST=%s)',
                output_skin_name,
                preferred_wear,
                'Y' if is_stattrak else 'N'
            )
        return None

    def _get_candidate_inputs(

        self,
        collection_name: str,
        input_rarity: str,
        *,
        is_stattrak: bool,
        max_float: float,
        limit: int,
        target_output_skin: Optional[str] = None,
        target_wear: Optional[str] = None,

    ) -> List[Dict]:

        """
        Get candidate input skins for trade-up contract.
        Args:
            collection_name: Name of the collection
            input_rarity: Rarity of input skins
            is_stattrak: Whether to search for StatTrak skins
            max_float: Maximum float value for input skins
            limit: Maximum number of skins to return
            target_output_skin: Optional target output skin name for float filtering
            target_wear: Optional target wear quality for float filtering
        Returns:
            List of candidate input skins with price, float, and wear data
        """
        # NEW: Calculate max_float if target specified
        if target_output_skin and target_wear:
            calculated_max_float = self._calculate_max_input_float_for_target_wear(
                target_output_skin,
                target_wear
            )
            if calculated_max_float is None:
                # Target wear is unachievable for this output skin
                return []
            max_float = calculated_max_float
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
        merged: List[Dict] = []
        seen = set()
        for it in list(expanded):
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
                            if price and float(price) > 0 and skin_float is not None:
                                merged.append({
                                    'name': nm,
                                    'collection': collection_name,
                                    'price': float(price),
                                    'float': float(skin_float),
                                    'wear': str(wear or ''),
                                    'rarity': rarity_norm,
                                    'instance_key': f"{nm}|dbg",
                                })
                                added_any = True
                                break
                        # РЈР±РёСЂР°РµРј СЃРїР°Рј Р»РѕРіРё HuntDebugInputs
                        # self._logger.info(
                        #     'HuntDebugInputs coll=%s rarity=%s ST=%s want_nm=%s present_before=%s candidates=%s added=%s',
                        #     str(collection_name),
                        #     str(rarity_norm),
                        #     'Y' if bool(is_stattrak) else 'N',
                        #     str(os.getenv('HUNT_DEBUG_EVAL_NAME', '') or ''),
                        #     'Y' if bool(has_nm) else 'N',
                        #     str(candidates[:5]),
                        #     'Y' if bool(added_any) else 'N',
                        # )
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
        with self._memo_lock:
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
        with self._memo_lock:
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
        with self._memo_lock:
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
        with self._memo_lock:
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
                    # If float not present, estimate from wear to avoid creating 10 identical None-floats.
                    lot_float = self._estimate_float_from_wear(wear, skin_name=str(name))
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
        with self._memo_lock:
            cached = self._memo_contract_eval.get(key)
            if cached is not None:
                try:
                    self._memo_contract_eval.move_to_end(key)
                except Exception:
                    pass
        if cached is not None:
            return dict(cached)
        val = self._calculate_contract_profit(contract_skins, target_collection, is_stattrak=is_stattrak)
        with self._memo_lock:
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
        with self._memo_lock:
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
            with self._memo_lock:
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
                with self._memo_lock:
                    self._memo_contract_craftability[key] = dict(result)
                return dict(result)
            cap = s.get('float')
            try:
                cap_f = float(cap) if cap is not None else None
            except Exception:
                cap_f = None
            if cap_f is None:
                cap_f = self._estimate_float_from_wear(s.get('wear'), skin_name=skin_name)
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
                    with self._memo_lock:
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
                lf = lot_float
                if lf is None:
                    lf = self._estimate_float_from_wear(wear, skin_name=skin_name)
                if lf is None:
                    if not bool(allow_unknown_float):
                        continue
                    lf = 1.0
                try:
                    lf2 = float(lf)
                except Exception:
                    continue
                normalized_lots.append((lf2, float(price), str(wear or '')))
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
                    with self._memo_lock:
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
        with self._memo_lock:
            self._memo_contract_craftability[key] = dict(result)
        return dict(result)

    def _collection_imbalance_ratio(self, collection_name: str, input_rarity: str, *, is_stattrak: bool) -> Optional[float]:

        input_rarity = self._normalize_rarity(input_rarity)
        memo_key = (collection_name, input_rarity, bool(is_stattrak))
        with self._memo_lock:
            cached = self._memo_collection_imbalance.get(memo_key)
        if cached is not None:
            return float(cached)
        best_wear = self._best_target_wear(collection_name, input_rarity, is_stattrak=is_stattrak)
        outs = self._get_possible_outputs(collection_name, input_rarity, is_stattrak=is_stattrak)
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
        with self._memo_lock:
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
        with self._memo_lock:
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
                # РЈР±РёСЂР°РµРј СЃРїР°Рј Р»РѕРіРё HuntDebug Revolution rarity (С‚РѕР»СЊРєРѕ РїСЂРё HUNT_DEBUG=1)
                if str(os.getenv('HUNT_DEBUG', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}:
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
            outs = self._get_possible_outputs(c, input_rarity, is_stattrak=is_stattrak)
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
                    # РЈР±РёСЂР°РµРј СЃРїР°Рј Р»РѕРіРё HuntDebug Revolution best_wear (С‚РѕР»СЊРєРѕ РїСЂРё HUNT_DEBUG=1)
                    if str(os.getenv('HUNT_DEBUG', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}:
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
                # Score = probability-weighted EV/cost ratio Г— imbalance skew.
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
        with self._memo_lock:
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
        with self._memo_lock:
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
        with self._memo_lock:
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
        with self._memo_lock:
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
        with self._memo_lock:
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
        # e.g. 0.5 means the "lottery ticket" covers в‰Ґ50% of cost in expectation.
        try:
            jackpot_min_ev_ratio = float(os.getenv('HUNT_JACKPOT_MIN_EV_RATIO', '0.5') or 0.5)
        except Exception:
            jackpot_min_ev_ratio = 0.5
        try:
            require_craftable = str(os.getenv('HUNT_REQUIRE_CRAFTABLE', '1') or '').strip().lower() not in {'0', 'false', 'no', 'off'}
        except Exception:
            require_craftable = True
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
                # NEW: Find achievable target wear with fallback logic
                achievable_result = self._find_achievable_target_wear(
                    target_skin,
                    best_wear,
                    target_c,
                    input_rarity,
                    is_stattrak=is_stattrak
                )
                if achievable_result is None:
                    # No achievable wear found for this target, skip it
                    continue
                target_wear, calculated_max_float = achievable_result
                # Use the achievable wear and calculated max_float
                # (this replaces the old best_wear and effective_max_float logic)
                max_float = self._wear_to_max_float(target_wear)
                _t1 = time.perf_counter() if prof else 0.0
                target_items = self._get_candidate_inputs(
                    target_c,
                    input_rarity,
                    is_stattrak=is_stattrak,
                    max_float=calculated_max_float,  # Use calculated max_float
                    limit=int(max(10, target_items_limit)),
                    target_output_skin=target_skin,  # NEW: Pass target for validation
                    target_wear=target_wear,          # NEW: Pass target wear
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
                    # Use target_wear for this collection so we select items whose float
                    # matches the optimal output wear rather than always pushing toward FN.
                    _prefer_wear_override = str(os.getenv('HUNT_PREFER_OUT_WEAR', '') or '').strip()
                    prefer_wear = _prefer_wear_override if _prefer_wear_override else target_wear
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
                                target_float_threshold=float(calculated_max_float) if float(calculated_max_float) < 0.999 else None,
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
                                    # РЈР±РёСЂР°РµРј СЃРїР°Рј Р»РѕРіРё HuntDebugEval (С‚РѕР»СЊРєРѕ РїСЂРё HUNT_DEBUG=1)
                                    if not should_log:
                                        raise RuntimeError('skip')
                                    # РџРѕРєР°Р·С‹РІР°РµРј РґРµС‚Р°Р»СЊРЅС‹Рµ Р»РѕРіРё С‚РѕР»СЊРєРѕ РїСЂРё HUNT_DEBUG=1
                                    if str(os.getenv('HUNT_DEBUG', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}:
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
                                        # РЈР±РёСЂР°РµРј СЃРїР°Рј Р»РѕРіРё HuntDebugEvalDetails (С‚РѕР»СЊРєРѕ РїСЂРё HUNT_DEBUG=1)
                                        if str(os.getenv('HUNT_DEBUG', '') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}:
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
                            # ratio=6 but prob=0.04 в†’ ev_ratio=0.24 < 0.5 в†’ NOT jackpot).
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
                            # Calculate expected wear for the hunt output skin
                            # Use the same logic as in calculate_contract_outcomes_details
                            hunt_output_name = str(current_best_out.get('name') or '')
                            avg_norm_float = float(current_eval.get('average_normalized_float') or 0.0)
                            # Get output skin data to calculate real float
                            output_skin_data = self.database.get_skin_by_name(hunt_output_name)
                            if output_skin_data:
                                try:
                                    out_min_f = float(output_skin_data.min_float)
                                    out_max_f = float(output_skin_data.max_float)
                                except Exception:
                                    out_min_f, out_max_f = 0.0, 1.0
                            else:
                                out_min_f, out_max_f = 0.0, 1.0
                            # Clamp values
                            if out_min_f < 0.0:
                                out_min_f = 0.0
                            if out_max_f > 1.0:
                                out_max_f = 1.0
                            if out_max_f <= out_min_f + 1e-9:
                                out_min_f, out_max_f = 0.0, 1.0
                            # Calculate real output float: Float_out = (Avg_norm * (Max - Min)) + Min
                            expected_out_float = avg_norm_float * (out_max_f - out_min_f) + out_min_f
                            expected_wear = self._determine_wear_from_float(expected_out_float)
                            item.update({
                                'target_collection': target_c,
                                'is_stattrak': bool(is_stattrak),
                                'input_skins': current,
                                'main_skins_count': int(target_cnt),
                                'filler_skins_count': int(filler_cnt),
                                'hunt_output': hunt_output_name,
                                'hunt_output_price': float(current_best_out.get('price') or 0.0),
                                'hunt_target_wear': target_wear,  # Use achievable target_wear
                                'hunt_expected_wear': expected_wear,  # Use calculated wear from real float
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
        # Two-pass refine (optional).
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
        refined: List[Dict] = []
        self._multisource_net_pricing = True
        # Threshold below which we perform a real-market liquidity check.
        # Configurable via env; default 0.035 вЂ” skins this clean are rare and expensive.
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
                    # в”Ђв”Ђ Liquidity check в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
                    # For skins whose required float в‰¤ threshold we verify real market
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
                    # Build a nameв†’index map to update prices after slippage calculation
                    _skin_real_prices: Dict[str, List[float]] = {} # name -> sorted real prices
                    for _skin_nm, _count_needed in _skin_slots.items():
                        _strictest_float = _skin_max_float.get(_skin_nm, 1.0)
                        if _strictest_float > float(_liq_float_threshold) + 1e-9:
                            # Normal float вЂ” no liquidity concern, keep original price
                            continue
                        # If CSFloat is unavailable, we can't verify real price for
                        # low-float skins вЂ” market cache price is unreliable for these.
                        # Skip the contract to avoid showing unrealistic profit.
                        _cfc = getattr(self.price_manager, 'csfloat_client', None)
                        _csfloat_available = (
                            _cfc is not None
                            and bool(getattr(_cfc, 'enabled', False))
                            and not getattr(_cfc, '_session_disabled', True)
                        )
                        if not _csfloat_available:
                            liquidity_ok = False
                            break
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
                    # в”Ђв”Ђ End liquidity check в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
                    # Calculate outcomes NOW вЂ” before any float/wear changes
                    # so they reflect the original contract float values
                    try:
                        pre_opt_outcomes = self.calculate_contract_outcomes_details(contract_skins, is_stattrak=is_st)
                        pre_opt_outcomes = sorted(pre_opt_outcomes or [], key=lambda x: float(x.get('price') or 0.0), reverse=True)
                    except Exception:
                        pre_opt_outcomes = None
                    refined_inputs = self._refine_contract_inputs(contract_skins, is_stattrak=is_st)
                    if refined_inputs:
                        contract_skins = refined_inputs
                    # в”Ђв”Ђ Float optimization (pushing to the edge of quality) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
                    try:
                        optimize_floats = str(os.getenv('HUNT_OPTIMIZE_FLOATS', '1') or '').strip().lower() not in {'0', 'false', 'no', 'off'}
                        if optimize_floats:
                            target_wear = str(r.get('hunt_target_wear') or 'Factory New')
                            # Wear-level optimization (no CSFloat needed)
                            wear_optimized = self._optimize_contract_wear_distribution(
                                contract_skins, target_wear=target_wear, is_stattrak=is_st
                            )
                            if wear_optimized:
                                contract_skins = wear_optimized
                            # Float-level optimization (CSFloat required)
                            optimized_skins = self._optimize_contract_floats(contract_skins, target_wear=target_wear, is_stattrak=is_st)
                            if optimized_skins:
                                contract_skins = optimized_skins
                    except Exception:
                        pass
                    # в”Ђв”Ђ End float optimization в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
                    # в”Ђв”Ђ FN Liquidity validation в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
                    # Uses CACHED data only - no API calls, very fast!
                    try:
                        fn_liquidity_check = str(os.getenv('HUNT_FN_LIQUIDITY_CHECK', '1') or '').strip().lower() not in {'0', 'false', 'no', 'off'}
                        if fn_liquidity_check:
                            if not self._validate_fn_contract_liquidity(contract_skins, is_stattrak=is_st):
                                # Contract failed FN liquidity check, skip it
                                continue
                    except Exception:
                        # Don't block on errors
                        pass
                    # в”Ђв”Ђ End FN liquidity validation в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
                    cd = self._calculate_contract_profit(contract_skins, target_collection, is_st)
                    out = dict(r)
                    out['input_skins'] = contract_skins
                    out.update(cd)
                    # Override outcomes with pre-optimization values вЂ” float optimization
                    # changes input floats to cheaper lots but must not affect output quality
                    if pre_opt_outcomes:
                        out['outcomes'] = pre_opt_outcomes
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

    def _validate_fn_contract_liquidity(self, contract_skins: List[Dict], *, is_stattrak: bool) -> bool:

        """
        Validates that FN contracts have sufficient liquidity on the market.
        Uses CACHED data only - no API calls, very fast!
        Returns False if contract should be filtered out due to liquidity issues.
        """
        if not contract_skins:
            return True
        # Check if contract has FN skins
        fn_skins = [s for s in contract_skins if s.get('wear') == 'Factory New']
        if not fn_skins:
            return True  # No FN skins, no need to check
        try:
            # Group skins by name to check required quantities and max float
            from collections import defaultdict
            skin_requirements = defaultdict(lambda: {'count': 0, 'max_float': 0.0, 'total_budget': 0.0})
            for s in fn_skins:
                skin_name = s.get('name', '')
                if skin_name:
                    skin_float = float(s.get('float', 0.07))
                    skin_price = float(s.get('price', 0.0))
                    req = skin_requirements[skin_name]
                    req['count'] += 1
                    req['max_float'] = max(req['max_float'], skin_float)
                    req['total_budget'] += skin_price
            # Check liquidity for each required FN skin using CACHED data only
            pm = self.price_manager
            market_client = pm.market_client if hasattr(pm, 'market_client') else None
            if not market_client:
                return True  # Can't check, assume OK
            for skin_name, req in skin_requirements.items():
                required_count = req['count']
                max_float = req['max_float']
                budget_per_skin = req['total_budget'] / required_count if req['total_budget'] > 0 else 0
                try:
                    # Get listings from CACHE (allow_refresh=False means no API calls!)
                    lots = market_client.get_listings(
                        skin_name,
                        target_wear='Factory New',
                        max_float=max_float,
                        exclude_stattrak=not bool(is_stattrak),
                        require_stattrak=bool(is_stattrak),
                        allow_refresh=False,  # IMPORTANT: Don't make API calls!
                        limit=required_count * 3,  # Get more than needed
                    )
                    if not lots:
                        # No skins available in cache
                        return False
                    # Check if we have enough skins
                    available_count = len(lots)
                    if available_count < required_count:
                        return False
                    # Check average price of first N skins
                    needed_lots = lots[:required_count]
                    avg_price = sum(float(lot[0]) for lot in needed_lots) / len(needed_lots)
                    # Check if price is reasonable (within budget + 15% margin)
                    if budget_per_skin > 0 and avg_price > budget_per_skin * 1.15:
                        return False
                except Exception:
                    # If we can't check this skin, reject the contract
                    return False
            return True
        except Exception:
            # If we can't validate, assume OK (don't block contracts)
            return True

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

    def _optimize_contract_wear_distribution(

        self,
        contract_skins: List[Dict],
        *,
        target_wear: str,
        is_stattrak: bool,

    ) -> Optional[List[Dict]]:

        """
        Optimizes wear distribution across contract slots without CSFloat.
        Uses market cache prices for 5 discrete wear levels.
        Returns list of (skin_name, optimal_wear, max_float) per slot,
        or None if no improvement found.
        Each slot gets the cheapest wear that keeps avg_norm within target.
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
        # Mid-float for each wear (used as norm proxy)
        wear_mid_float = {
            'Factory New':   0.035,
            'Minimal Wear':  0.110,
            'Field-Tested':  0.260,
            'Well-Worn':     0.415,
            'Battle-Scarred': 0.725,
        }
        wear_order = ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']
        target_max_avg_norm = float(wear_thresholds.get(target_wear, 0.07))
        # Find bottleneck outcome
        try:
            outcomes = self.calculate_contract_outcomes_details(contract_skins, is_stattrak=is_stattrak)
        except Exception:
            return None
        if not outcomes:
            return None
        limit_avg_norm = 1.0
        for o in outcomes:
            min_f = float(o.get('min_float', 0.0))
            max_f = float(o.get('max_float', 1.0))
            denom = max_f - min_f
            if denom > 1e-9:
                max_norm = (target_max_avg_norm - min_f) / denom
                if max_norm < limit_avg_norm:
                    limit_avg_norm = max_norm
        target_avg_norm = max(0.0, limit_avg_norm * 0.998)
        float_budget = target_avg_norm * 10.0
        # Build per-slot data: get price for each wear level from market cache
        pm = self.price_manager
        slots = []
        for s in contract_skins:
            nm = str(s.get('name') or '')
            skin_data = self.database.get_skin_by_name(nm)
            if not skin_data:
                return None  # Can't optimize without skin data
            min_f = float(skin_data.min_float)
            max_f = float(skin_data.max_float)
            denom = max_f - min_f if max_f > min_f else 1.0
            # Get price for each available wear from market cache
            wear_prices = {}
            available_wears = list(getattr(skin_data, 'wears', None) or [])
            for w in wear_order:
                if available_wears and w not in available_wears:
                    continue
                mid_f = wear_mid_float[w]
                # Clamp to skin range
                if mid_f < min_f or mid_f > max_f:
                    continue
                try:
                    lots = pm.market_client.get_listings(
                        nm,
                        target_wear=w,
                        exclude_stattrak=not bool(is_stattrak),
                        require_stattrak=bool(is_stattrak),
                        allow_refresh=False,
                        limit=3,
                    )
                    if lots:
                        wear_prices[w] = float(lots[0][0])
                except Exception:
                    pass
            if not wear_prices:
                return None
            curr_f = float(s.get('float', mid_f))
            curr_norm = max(0.0, min(1.0, (curr_f - min_f) / denom))
            slots.append({
                'name': nm,
                'min_f': min_f,
                'max_f': max_f,
                'denom': denom,
                'curr_norm': curr_norm,
                'curr_price': float(s.get('price', 0.0)),
                'curr_wear': str(s.get('wear') or ''),
                'wear_prices': wear_prices,  # {wear: price}
            })
        if len(slots) != 10:
            return None
        # Build discrete curve per slot: (price, norm, wear, max_float)
        # norm = mid_float normalized to skin range

        def slot_curve(sl):
            pts = []
            for w, price in sl['wear_prices'].items():
                mid_f = wear_mid_float[w]
                norm = max(0.0, min(1.0, (mid_f - sl['min_f']) / sl['denom']))
                # Use slightly below boundary so float stays within wear quality
                max_f_wear = wear_thresholds[w] - (0.0001 if w != 'Battle-Scarred' else 0.0)
                # Clamp to skin range
                max_f_wear = min(max_f_wear, sl['max_f'])
                # norm for max_float (used for final validation)
                max_norm = max(0.0, min(1.0, (max_f_wear - sl['min_f']) / sl['denom']))
                pts.append((price, norm, w, max_f_wear, max_norm))
            pts.sort(key=lambda x: x[1])  # sort by norm ascending
            return pts
        # Greedy: start from current norm, distribute remaining budget
        # to slots where raising norm (dirtier = cheaper) saves most per norm unit
        assigned = [sl['curr_norm'] for sl in slots]
        remaining = float_budget - sum(assigned)
        if remaining < 1e-4:
            return None  # Already at budget limit, no room to optimize
        MAX_ITER = 30
        for _ in range(MAX_ITER):
            if remaining < 1e-4:
                break
            best_eff = 0.0
            best_slot = None
            best_pt = None
            for i, sl in enumerate(slots):
                curve = slot_curve(sl)
                curr_norm = assigned[i]
                # Current price at current norm
                curr_pts = [pt for pt in curve if pt[1] <= curr_norm + 1e-6]
                if not curr_pts:
                    continue
                curr_price = min(curr_pts, key=lambda x: x[0])[0]
                for price, norm, wear, max_f_wear, max_norm in curve:
                    if norm <= curr_norm + 1e-6:
                        continue  # not dirtier
                    norm_delta = norm - curr_norm
                    if norm_delta > remaining + 1e-6:
                        continue  # exceeds budget
                    gain = curr_price - price
                    if gain <= 0:
                        continue
                    eff = gain / norm_delta
                    if eff > best_eff:
                        best_eff = eff
                        best_slot = i
                        best_pt = (price, norm, wear, max_f_wear, max_norm)
            if best_slot is None:
                break
            norm_delta = best_pt[1] - assigned[best_slot]
            assigned[best_slot] = best_pt[1]
            remaining -= norm_delta
        # Build result: assign optimal wear to each slot
        original_total = sum(sl['curr_price'] for sl in slots)
        result = []
        new_total = 0.0
        for i, sl in enumerate(slots):
            target_norm = assigned[i]
            curve = slot_curve(sl)
            # Find cheapest wear at or below target_norm
            candidates = [(p, n, w, mf, mn) for p, n, w, mf, mn in curve if n <= target_norm + 1e-6]
            if not candidates:
                result.append(dict(contract_skins[i]))
                new_total += sl['curr_price']
                continue
            best = min(candidates, key=lambda x: x[0])
            price, norm, wear, max_float_for_wear, max_norm_for_wear = best
            new_total += price
            s2 = dict(contract_skins[i])
            s2['wear'] = wear
            s2['price'] = price
            # Use mid_float for avg_norm calculation (conservative estimate)
            s2['float'] = wear_mid_float[wear]
            s2['max_float_for_wear'] = max_float_for_wear
            result.append(s2)
        if new_total >= original_total - 0.01:
            return None  # No improvement
        # Final validation using max_float (worst case) вЂ” ensures quality is guaranteed
        # even if buyer purchases at the maximum allowed float
        validation_skins = []
        for s in result:
            s_val = dict(s)
            if s_val.get('max_float_for_wear') is not None:
                s_val['float'] = float(s_val['max_float_for_wear'])
            validation_skins.append(s_val)
        final_avg_norm = self._calculate_average_normalized_float(validation_skins)
        if final_avg_norm > target_avg_norm + 1e-4:
            return None  # Violated constraint even at max allowed float
        return result

    def _fetch_price_curve(self, skin_name: str, *, is_stattrak: bool, skin_min_f: float, skin_max_f: float) -> List[tuple]:

        """
        Fetches price curve for a skin: list of (price, norm_float) sorted by norm_float ascending.
        Uses CSFloat ONLY вЂ” market cache floats are approximate (mid-wear) and cause wrong results.
        Returns empty list if CSFloat is unavailable.
        Results are stored in session dedup cache.
        """
        if not hasattr(self, '_float_opt_session_cache'):
            self._float_opt_session_cache: Dict[tuple, object] = {}
        curve_key = ('curve', skin_name, bool(is_stattrak))
        if curve_key in self._float_opt_session_cache:
            return self._float_opt_session_cache[curve_key]  # type: ignore
        pm = self.price_manager
        curve: List[tuple] = []  # (price, norm_float)
        # CSFloat only вЂ” market cache has approximate floats (mid-wear) which break optimization
        csfloat = getattr(pm, 'csfloat_client', None)
        if csfloat and bool(getattr(csfloat, 'enabled', False)) and not getattr(csfloat, '_session_disabled', True):
            try:
                lots = csfloat.get_listings(
                    skin_name,
                    target_wear=None,
                    max_float=None,
                    exclude_stattrak=not bool(is_stattrak),
                    require_stattrak=bool(is_stattrak),
                    limit=30,
                )
                denom = skin_max_f - skin_min_f
                for price, fval, _ in lots:
                    if fval is None or denom <= 1e-9:
                        continue
                    norm = max(0.0, min(1.0, (float(fval) - skin_min_f) / denom))
                    curve.append((float(price), norm))
            except Exception:
                curve = []
        # Sort by norm_float ascending
        curve.sort(key=lambda x: x[1])
        self._float_opt_session_cache[curve_key] = curve
        return curve

    def _cheapest_lot_at_max_norm(self, curve: List[tuple], max_norm: float) -> Optional[tuple]:

        """Returns cheapest (price, norm_float) from curve where norm_float <= max_norm."""
        candidates = [(p, n) for p, n in curve if n <= max_norm + 1e-6]
        if not candidates:
            return None
        return min(candidates, key=lambda x: x[0])

    def _optimize_contract_floats(self, contract_skins: List[Dict], *, target_wear: str, is_stattrak: bool) -> Optional[List[Dict]]:

        """
        Optimizes float distribution across contract slots using CSFloat data.
        Requires CSFloat вЂ” skipped entirely if CSFloat is unavailable or disabled,
        because market cache floats are approximate and produce wrong results.
        """
        if not contract_skins or len(contract_skins) != 10:
            return None
        # Only run if CSFloat is available with real float data
        pm = self.price_manager
        csfloat = getattr(pm, 'csfloat_client', None)
        csfloat_ok = (
            csfloat is not None
            and bool(getattr(csfloat, 'enabled', False))
            and not getattr(csfloat, '_session_disabled', True)
        )
        if not csfloat_ok:
            return None
        wear_thresholds = {
            'Factory New': 0.07,
            'Minimal Wear': 0.15,
            'Field-Tested': 0.38,
            'Well-Worn': 0.45,
            'Battle-Scarred': 1.0,
        }
        target_max_avg_norm = float(wear_thresholds.get(target_wear, 0.07))
        current_skins = [dict(s) for s in contract_skins]
        outcomes = self.calculate_contract_outcomes_details(current_skins, is_stattrak=is_stattrak)
        if not outcomes:
            return None
        # Find bottleneck outcome вЂ” the one that constrains avg_norm the most
        limit_avg_norm = 1.0
        for o in outcomes:
            min_f = float(o.get('min_float', 0.0))
            max_f = float(o.get('max_float', 1.0))
            denom = max_f - min_f
            if denom > 1e-9:
                max_norm_for_this = (target_max_avg_norm - min_f) / denom
                if max_norm_for_this < limit_avg_norm:
                    limit_avg_norm = max_norm_for_this
        # Safety margin
        target_avg_norm = max(0.0, limit_avg_norm * 0.998)
        # Store for UI
        for s in current_skins:
            s['target_max_avg_float'] = round(target_avg_norm, 6)
        float_budget = target_avg_norm * 10.0  # total norm budget across all 10 slots
        # в”Ђв”Ђ Build per-slot data в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        slots = []
        for s in current_skins:
            nm = str(s.get('name', ''))
            skin_data = self.database.get_skin_by_name(nm)
            if not skin_data:
                slots.append(None)
                continue
            try:
                min_f = float(skin_data.min_float)
                max_f = float(skin_data.max_float)
            except Exception:
                slots.append(None)
                continue
            if max_f <= min_f + 1e-9:
                slots.append(None)
                continue
            curve = self._fetch_price_curve(nm, is_stattrak=is_stattrak, skin_min_f=min_f, skin_max_f=max_f)
            curr_f = float(s.get('float', 0.0))
            curr_norm = max(0.0, min(1.0, (curr_f - min_f) / (max_f - min_f)))
            slots.append({
                'skin': s,
                'name': nm,
                'min_f': min_f,
                'max_f': max_f,
                'curr_norm': curr_norm,
                'curr_price': float(s.get('price', 0.0)),
                'curve': curve,
            })
        # в”Ђв”Ђ Greedy redistribution в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        # Phase 1: assign minimum norm to each slot (cheapest available lot)
        # Phase 2: distribute remaining budget to slots where it saves money
        valid_indices = [i for i, sl in enumerate(slots) if sl is not None]
        if not valid_indices:
            return None
        # Start: assign current norm to all valid slots as baseline
        assigned_norm = [0.0] * 10
        for i in valid_indices:
            sl = slots[i]
            assigned_norm[i] = sl['curr_norm']
        remaining_budget = float_budget - sum(assigned_norm[i] for i in valid_indices)
        # Phase 2: greedily give budget to slots where raising norm saves most money
        # For each slot, compute marginal savings: price(curr_norm) - price(max_norm)
        # Prioritize slots with highest savings per norm unit used
        MAX_ITERATIONS = 50
        for _ in range(MAX_ITERATIONS):
            if remaining_budget < 1e-4:
                break
            best_gain = 0.0  # efficiency threshold: savings per norm unit
            best_slot = None
            best_new_norm = None
            best_new_price = None
            for i in valid_indices:
                sl = slots[i]
                if not sl['curve']:
                    continue
                curr_assigned = assigned_norm[i]
                # Max norm this slot can take without exceeding budget
                max_norm_for_slot = min(1.0, curr_assigned + remaining_budget)
                # Current price at current assigned norm
                curr_lot = self._cheapest_lot_at_max_norm(sl['curve'], curr_assigned)
                curr_price_at_norm = curr_lot[0] if curr_lot else sl['curr_price']
                # Try ALL available lots within budget вЂ” pick best savings/norm ratio
                for lot_price, lot_norm in sl['curve']:
                    if lot_norm <= curr_assigned + 1e-6:
                        continue  # not an improvement
                    if lot_norm > max_norm_for_slot + 1e-6:
                        continue  # exceeds budget
                    norm_used = lot_norm - curr_assigned
                    if norm_used < 1e-6:
                        continue
                    gain = curr_price_at_norm - lot_price
                    if gain <= 0.01:
                        continue
                    # savings per norm unit вЂ” prefer efficient use of budget
                    efficiency = gain / norm_used
                    if efficiency > best_gain:
                        best_gain = efficiency
                        best_slot = i
                        best_new_norm = lot_norm
                        best_new_price = lot_price
            if best_slot is None:
                break
            # Apply: raise norm for best_slot, consume budget
            norm_delta = best_new_norm - assigned_norm[best_slot]
            assigned_norm[best_slot] = best_new_norm
            remaining_budget -= norm_delta
        # в”Ђв”Ђ Apply results в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        changed = False
        original_total = sum(slots[i]['curr_price'] for i in valid_indices)
        new_total = 0.0
        for i in valid_indices:
            sl = slots[i]
            target_norm = assigned_norm[i]
            # Find actual lot to buy at this norm
            best_lot = self._cheapest_lot_at_max_norm(sl['curve'], target_norm)
            if best_lot is None:
                new_total += sl['curr_price']
                continue
            new_price, new_norm = best_lot
            new_total += new_price
        # Only apply if total cost is lower
        if new_total >= original_total - 0.01:
            return None
        for i in valid_indices:
            sl = slots[i]
            target_norm = assigned_norm[i]
            best_lot = self._cheapest_lot_at_max_norm(sl['curve'], target_norm)
            if best_lot is None:
                continue
            new_price, new_norm = best_lot
            new_float = new_norm * (sl['max_f'] - sl['min_f']) + sl['min_f']
            if new_float < 0.07:
                new_wear = 'Factory New'
            elif new_float < 0.15:
                new_wear = 'Minimal Wear'
            elif new_float < 0.38:
                new_wear = 'Field-Tested'
            elif new_float < 0.45:
                new_wear = 'Well-Worn'
            else:
                new_wear = 'Battle-Scarred'
            current_skins[i].update({
                'price': new_price,
                'float': round(new_float, 6),
                'wear': new_wear,
            })
            changed = True
        if not changed:
            return None
        # Final validation: ensure avg_norm is still within target after optimization
        final_avg_norm = self._calculate_average_normalized_float(current_skins)
        if final_avg_norm > target_avg_norm + 1e-4:
            # Optimization violated the float constraint вЂ” discard results
            return None
        return current_skins

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
        outputs = self._get_possible_outputs(collection_name, input_rarity, is_stattrak=is_stattrak)
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

    def _estimate_float_from_wear(self, wear: Optional[str], skin_name: Optional[str] = None) -> Optional[float]:

        """
        Estimate float value from wear quality.
        If skin_name is provided, clamps the estimate to the skin's actual float range.
        """
        if not wear:
            return None
        w = str(wear)
        # Default estimates (midpoint of each wear range)
        if w == 'Factory New':
            estimated = 0.035
        elif w == 'Minimal Wear':
            estimated = 0.11
        elif w == 'Field-Tested':
            estimated = 0.26
        elif w == 'Well-Worn':
            estimated = 0.405
        elif w == 'Battle-Scarred':
            estimated = 0.725
        else:
            return None
        # Clamp to skin's actual float range if skin_name provided
        if skin_name:
            skin_data = self.database.get_skin_by_name(skin_name)
            if skin_data:
                try:
                    min_f = float(skin_data.min_float)
                    max_f = float(skin_data.max_float)
                    # Clamp estimated value to skin's range
                    if estimated < min_f:
                        estimated = min_f
                    elif estimated > max_f:
                        # Use a value near max_f but not exactly at the edge
                        estimated = max_f - 0.01
                        if estimated < min_f:
                            estimated = (min_f + max_f) / 2.0
                except Exception:
                    pass
        return estimated

    def _wear_for_max_float(self, max_float: Optional[float]) -> Optional[str]:

        if max_float is None:
            return None
        try:
            mf = float(max_float)
        except Exception:
            return None
        # Р’С‹Р±РёСЂР°РµРј "Р»СѓС‡С€РµРµ" РєР°С‡РµСЃС‚РІРѕ, РєРѕС‚РѕСЂРѕРµ РіР°СЂР°РЅС‚РёСЂРѕРІР°РЅРЅРѕ <= max_float
        if mf <= 0.07 + 1e-12:
            return 'Factory New'
        if mf <= 0.15 + 1e-12:
            return 'Minimal Wear'
        if mf <= 0.38 + 1e-12:
            return 'Field-Tested'
        if mf <= 0.45 + 1e-12:
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
        with self._memo_lock:
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
            with self._memo_lock:
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
                with self._memo_lock:
                    self._memo_price_with_float[key_relaxed] = val
        with self._memo_lock:
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
        with self._memo_lock:
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
            with self._memo_lock:
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
                with self._memo_lock:
                    self._memo_price[key_relaxed] = val
        with self._memo_lock:
            self._memo_price[key] = val
        return val

    def _calculate_average_float(self, contract_skins: List[Dict]) -> float:
        """Расчет среднего флоута контракта"""
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

    def _calculate_average_normalized_float(self, contract_skins: List[Dict]) -> float:
        """Средний нормализованный float входа (0..1) с учетом min/max диапазона каждого скина."""
        if not contract_skins:
            return 0.0

        total = 0.0
        valid = 0
        for skin in contract_skins:
            skin_float = skin.get('float', None)
            if skin_float is None:
                continue

            skin_data = self.database.get_skin_by_name(skin.get('name', ''))
            if not skin_data:
                continue

            try:
                min_f = float(skin_data.min_float)
                max_f = float(skin_data.max_float)
            except Exception:
                continue

            # Clamp skin_float to valid range BEFORE normalization
            skin_float_clamped = float(skin_float)
            if skin_float_clamped < min_f:
                skin_float_clamped = min_f
            elif skin_float_clamped > max_f:
                skin_float_clamped = max_f

            denom = max_f - min_f
            if denom <= 1e-9:
                continue

            norm = (skin_float_clamped - min_f) / denom
            if norm < 0.0:
                norm = 0.0
            if norm > 1.0:
                norm = 1.0

            total += norm
            valid += 1

        return total / valid if valid > 0 else 0.0

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
            # Apply fee consistently: during refine _multisource_net_pricing=True and price is
            # already net; during initial eval price is gross so we must deduct the fee before
            # comparing to input_cost. Without this, gross-price contracts appear PP=100% but
            # then fail the post-refine filter after the fee is applied.
            eff_price = float(price) if bool(self._multisource_net_pricing) else float(price) * (1.0 - float(self.market_fee))
            if eff_price > input_cost:
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

        # Align with external calculators: wear is determined by the normalized average float (f')
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

    def _calculate_weighted_average_normalized_float(self, contract_skins: List[Dict], is_stattrak: bool) -> float:
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
                continue

            skin_name = skin.get('name', '')
            skin_data = self.database.get_skin_by_name(skin_name)
            if not skin_data:
                continue

            try:
                min_f = float(skin_data.min_float)
                max_f = float(skin_data.max_float)
            except Exception:
                continue

            # Clamp skin_float to valid range BEFORE normalization
            skin_float_clamped = float(skin_float)
            if skin_float_clamped < min_f:
                skin_float_clamped = min_f
            elif skin_float_clamped > max_f:
                skin_float_clamped = max_f

            denom = max_f - min_f
            if denom <= 1e-9:
                continue

            norm = (skin_float_clamped - min_f) / denom
            if norm < 0.0:
                norm = 0.0
            if norm > 1.0:
                norm = 1.0

            outcomes_count = self._get_next_grade_skins_count(
                skin.get('collection', ''),
                input_rarity,
                is_stattrak=is_stattrak,
            )
            if outcomes_count <= 0:
                continue

            weighted_sum += norm * float(outcomes_count)
            total_weight += float(outcomes_count)

        if total_weight <= 0:
            return 0.0
        return weighted_sum / total_weight

    def _determine_wear_from_float(self, item_float: float) -> str:
        if item_float < 0.07:
            return 'Factory New'
        elif item_float < 0.15:
            return 'Minimal Wear'
        elif item_float < 0.38:
            return 'Field-Tested'
        elif item_float < 0.45:
            return 'Well-Worn'
        else:
            return 'Battle-Scarred'

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

                price_info = None
                if max_float_threshold is not None and float(max_float_threshold) < 0.999:
                    # Сначала пытаемся найти лот, который реально проходит по float.
                    price_info = self._cached_get_price_with_float(
                        skin.name,
                        max_float=max_float_threshold,
                        exclude_stattrak=not is_stattrak,
                        require_stattrak=bool(is_stattrak),
                        strict_name_match=True,
                        allow_refresh=False,
                    )
                    if not price_info:
                        price_info = self._cached_get_price_with_float(
                            skin.name,
                            max_float=max_float_threshold,
                            exclude_stattrak=not is_stattrak,
                            require_stattrak=bool(is_stattrak),
                            strict_name_match=False,
                            allow_refresh=False,
                        )
                if not price_info:
                    price_info = self._cached_get_price_with_float(
                        skin.name,
                        target_wear=None,
                        max_float=None,
                        exclude_stattrak=not is_stattrak,
                        require_stattrak=bool(is_stattrak),
                        strict_name_match=True,
                        allow_refresh=False,
                    )
                if not price_info:
                    price_info = self._cached_get_price_with_float(
                        skin.name,
                        target_wear=None,
                        max_float=None,
                        exclude_stattrak=not is_stattrak,
                        require_stattrak=bool(is_stattrak),
                        strict_name_match=False,
                        allow_refresh=False,
                    )
                if not price_info:
                    continue
                price, skin_float, wear = price_info
                if skin_float is None and max_float_threshold is not None and float(max_float_threshold) < 0.999:
                    # v2 prices: float часто отсутствует. Не подставляем порог автоматически,
                    # иначе можно взять BS/WW по цене и ошибочно считать его MW.
                    allowed_wears = ["Factory New", "Minimal Wear"]
                    if float(max_float_threshold) >= 0.38:
                        allowed_wears.append("Field-Tested")
                    if wear in allowed_wears:
                        if wear == "Factory New":
                            skin_float = 0.07
                        elif wear == "Minimal Wear":
                            skin_float = 0.15
                        else:
                            skin_float = 0.38
                    else:
                        continue
                if skin_float is None:
                    skin_float = self._estimate_float_from_wear(wear, skin_name=skin.name)
                if skin_float is None:
                    continue
                if max_price is not None and price is not None and float(price) > float(max_price):
                    continue
                if max_float_threshold is not None and float(skin_float) > float(max_float_threshold):
                    continue

                outcomes_count = self._get_next_grade_skins_count(filler_collection, rarity, is_stattrak)
                if outcomes_count <= 0:
                    continue

                candidate_skins.append({
                    'name': skin.name,
                    'collection': filler_collection,
                    'price': float(price),
                    'float': float(skin_float),
                    'wear': wear,
                    'rarity': self._normalize_rarity(skin.rarity),
                    'outcomes_count': int(outcomes_count),
                    'efficiency': 0.0,
                })

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

    def _get_main_skins(self, collection: str, count: int, is_stattrak: bool, rarity: Optional[str] = None,
                        max_float: float = 1.0) -> List[Dict]:
        """Получение самых дешевых основных скинов из коллекции"""
        rarity_norm = self._normalize_rarity(rarity) if rarity else None
        mf = round(float(max_float), 4) if max_float is not None else None
        memo_key = (collection, int(count), bool(is_stattrak), rarity_norm, mf)
        with self._memo_lock:
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
            # Важно: если всегда брать самый дешевый лот без ограничений, чаще всего
            # это будет Battle-Scarred с высоким float, и контракты станут невозможны.
            # Поэтому сначала пытаемся подобрать лот с ограничением max_float.
            price_info = None
            if max_float is not None:
                price_info = self._cached_get_price_with_float(
                    skin.name,
                    max_float=max_float,
                    exclude_stattrak=not is_stattrak,
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=True,
                    allow_refresh=False,
                )
                if not price_info:
                    price_info = self._cached_get_price_with_float(
                        skin.name,
                        max_float=max_float,
                        exclude_stattrak=not is_stattrak,
                        require_stattrak=bool(is_stattrak),
                        strict_name_match=False,
                        allow_refresh=False,
                    )
            if not price_info:
                price_info = self._cached_get_price_with_float(
                    skin.name,
                    # Не делаем max_float обязательным: если float отсутствует в прайсах,
                    # берём лот и дальше попробуем оценить float по wear.
                    target_wear=None,
                    max_float=None,
                    exclude_stattrak=not is_stattrak,
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=True,
                    allow_refresh=False,
                )
            if not price_info:
                price_info = self._cached_get_price_with_float(
                    skin.name,
                    target_wear=None,
                    max_float=None,
                    exclude_stattrak=not is_stattrak,
                    require_stattrak=bool(is_stattrak),
                    strict_name_match=False,
                    allow_refresh=False,
                )
            if not price_info:
                continue
            price, skin_float, wear = price_info
            if skin_float is None and max_float is not None and float(max_float) < 0.999:
                # v2 prices: float часто отсутствует. Не подставляем порог автоматически,
                # иначе можно взять Battle-Scarred по цене и ошибочно считать его MW.
                # Разрешаем только если wear явно в рамках порога.
                allowed_wears = ["Factory New", "Minimal Wear"]
                if float(max_float) >= 0.38:
                    allowed_wears.append("Field-Tested")
                if wear in allowed_wears:
                    if wear == "Factory New":
                        skin_float = 0.07
                    elif wear == "Minimal Wear":
                        skin_float = 0.15
                    else:
                        skin_float = 0.38
                else:
                    continue
            if skin_float is None:
                skin_float = self._estimate_float_from_wear(wear, skin_name=skin.name)
            if skin_float is None:
                continue
            if max_float is not None and float(skin_float) > float(max_float):
                continue
            if price and price > 0:
                priced_skins.append({
                    'name': skin.name,
                    'collection': collection,
                    'price': price,
                    'float': skin_float,
                    'wear': wear,
                    'rarity': self._normalize_rarity(skin.rarity)
                })

        priced_skins.sort(key=lambda x: x['price'])
        result = priced_skins[:count]
        with self._memo_lock:
            self._memo_main_skins[memo_key] = list(result)
        return result

    def _get_next_grade_skins_count(self, collection: str, input_rarity: str, is_stattrak: bool) -> int:
        """Количество скинов следующего грейда для данного входного грейда в коллекции."""
        input_rarity = self._normalize_rarity(input_rarity)
        next_rarity = self._get_next_rarity(input_rarity)
        if not next_rarity:
            return 0

        memo_key = (collection, input_rarity, next_rarity, bool(is_stattrak))
        with self._memo_lock:
            cached = self._memo_next_grade_count.get(memo_key)
        if cached is not None:
            return int(cached)

        collection_skins = self.database.get_collection_skins(collection)
        count = 0
        for skin in collection_skins:
            if self._normalize_rarity(skin.rarity) != next_rarity:
                continue
            count += 1

        with self._memo_lock:
            self._memo_next_grade_count[memo_key] = int(count)
        return int(count)

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

    def _get_possible_outputs(self, collection: str, input_rarity: str, is_stattrak: bool) -> List[Dict]:
        """Получение возможных выходных скинов для конкретной коллекции и входного грейда."""
        input_rarity = self._normalize_rarity(input_rarity)
        next_rarity = self._get_next_rarity(input_rarity)
        if not next_rarity:
            return []

        memo_key = (collection, input_rarity, next_rarity, bool(is_stattrak))
        with self._memo_lock:
            cached = self._memo_possible_outputs.get(memo_key)
        if cached is not None:
            return list(cached)

        collection_skins = self.database.get_collection_skins(collection)

        possible_outputs = []
        for skin in collection_skins:
            if self._normalize_rarity(skin.rarity) != next_rarity:
                continue

            possible_outputs.append({
                'name': skin.name,
                'rarity': skin.rarity
            })

        with self._memo_lock:
            self._memo_possible_outputs[memo_key] = list(possible_outputs)
        return possible_outputs

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
            
            # Получаем цену и float
            price_info = self._cached_get_price_with_float(
                skin.name,
                max_float=None,
                exclude_stattrak=not is_stattrak,
                require_stattrak=bool(is_stattrak),
                strict_name_match=False,
                allow_refresh=False,
            )
            
            if price_info:
                price, skin_float, wear = price_info
                if skin_float is None:
                    skin_float = self._estimate_float_from_wear(wear, skin_name=skin.name)
                if skin_float is None:
                    continue
                if max_price is not None and price is not None and price > max_price:
                    continue
                if skin_float < target_float_threshold:
                    outcomes_count = self._get_next_grade_skins_count(skin.collection, rarity, is_stattrak)
                    if outcomes_count <= 0:
                        continue
                    efficiency = 0.0
                    candidate_skins.append({
                        'name': skin.name,
                        'collection': skin.collection,
                        'price': price,
                        'float': skin_float,
                        'wear': wear,
                        'rarity': self._normalize_rarity(skin.rarity),
                        'outcomes_count': outcomes_count,
                        'efficiency': efficiency
                    })
        
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

    def calculate_contract_outcomes_details(self, contract_skins: List[Dict], is_stattrak: bool) -> List[Dict]:
        avg_norm = self._calculate_average_normalized_float(contract_skins)
        if avg_norm < 0.0:
            avg_norm = 0.0
        if avg_norm > 1.0:
            avg_norm = 1.0
        input_rarity = contract_skins[0].get('rarity') if contract_skins else None
        if not input_rarity:
            return []

        collections_count = defaultdict(int)
        for skin in contract_skins:
            collections_count[skin['collection']] += 1

        output_items_by_collection: Dict[str, List[Dict]] = {}
        for collection_name, skins_count in collections_count.items():
            output_items = self._get_possible_outputs(collection_name, input_rarity, is_stattrak=is_stattrak)
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
                    wears_avail = list(getattr(skin_data, 'wears', None) or []) if skin_data else []
                except Exception:
                    wears_avail = []

                if min_f < 0.0:
                    min_f = 0.0
                if max_f > 1.0:
                    max_f = 1.0
                if max_f <= min_f + 1e-9:
                    min_f, max_f = 0.0, 1.0

                out_float = float(avg_norm) * (max_f - min_f) + min_f
                wear = self._determine_wear_from_float(out_float)

                # Ensure the computed wear exists for this skin. If not, degrade to the nearest worse available wear.
                if wears_avail and wear not in wears_avail:
                    wear_order = ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']
                    try:
                        start_i = wear_order.index(wear)
                    except Exception:
                        start_i = 0
                    chosen = None
                    for j in range(start_i, len(wear_order)):
                        if wear_order[j] in wears_avail:
                            chosen = wear_order[j]
                            break
                    if chosen is not None:
                        wear = chosen

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

    def clear_price_memoization(self) -> None:

        with self._memo_lock:
            self._memo_price.clear()
            self._memo_price_with_float.clear()
            self._memo_listings.clear()
            self._memo_effective_sell_price.clear()
            self._memo_contract_eval.clear()
            self._memo_contract_craftability.clear()
            self._memo_collection_avg_outcome_price.clear()
            self._memo_collection_score.clear()
            self._memo_collection_imbalance.clear()

    def _normalize_rarity(self, rarity_name: Optional[str]) -> Optional[str]:

        if not rarity_name:
            return rarity_name
        try:
            return self.database._normalize_rarity(rarity_name)
        except Exception:
            return rarity_name

