import threading
import time
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import logging
import os

from dotenv import load_dotenv, dotenv_values

from database import CS2Database
from api_client import PriceManager
from calculator import ContractCalculator


logger = logging.getLogger(__name__)


def _load_env_file_manual(dotenv_path: Path) -> dict:
    vals = {}
    try:
        raw = None
        try:
            raw = dotenv_path.read_text(encoding='utf-8-sig')
        except Exception:
            raw = dotenv_path.read_text(encoding='cp1251')
        for line in (raw or '').splitlines():
            s = (line or '').strip()
            if not s or s.startswith('#'):
                continue
            if '=' not in s:
                continue
            k, v = s.split('=', 1)
            k = (k or '').strip()
            v = (v or '').strip()
            if not k:
                continue
            if v and ((v[0] == v[-1]) and v[0] in {'"', "'"}):
                v = v[1:-1]
            vals[k] = v
    except Exception:
        return {}
    return vals


class TargetHuntingService:
    def __init__(self):
        dotenv_path = Path(__file__).resolve().with_name('.env')
        load_dotenv(dotenv_path=dotenv_path, override=True)
        try:
            if os.getenv('HUNT_DEBUG') is None:
                vals = dotenv_values(dotenv_path)
                has_hunt_debug = 'HUNT_DEBUG' in (vals or {})
                if not (vals or {}):
                    vals = _load_env_file_manual(dotenv_path)
                for k, v in (vals or {}).items():
                    if v is None:
                        continue
                    if os.getenv(k) is None:
                        os.environ[str(k)] = str(v)
                if not has_hunt_debug:
                    try:
                        has_hunt_debug = 'HUNT_DEBUG' in (vals or {})
                    except Exception:
                        has_hunt_debug = False
                try:
                    hunt_keys = [repr(k) for k in (vals or {}).keys() if str(k).upper().startswith('HUNT')]
                except Exception:
                    hunt_keys = []
                logging.getLogger().info(
                    'TargetHuntingService env fallback used: parsed_keys=%s hunt_debug_key_present=%s hunt_keys=%s',
                    int(len(vals or {})),
                    'Y' if has_hunt_debug else 'N',
                    str(hunt_keys),
                )
        except Exception:
            pass
        logging.getLogger().info('TargetHuntingService env loaded: %s (HUNT_DEBUG=%s)', str(dotenv_path), str(os.getenv('HUNT_DEBUG')))
        self.database = CS2Database()
        self.price_manager = PriceManager()
        self.calculator: Optional[ContractCalculator] = None

        self._cache_lock = threading.Lock()
        self._cache: Dict[str, Dict] = {}
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._refresh_running = threading.Event()

    def initialize(self) -> None:
        logger.info('Initializing TargetHuntingService...')
        if not self.database.load_data():
            raise RuntimeError('Failed to load database')
        pm_start = time.time()
        if not self.price_manager.initialize():
            raise RuntimeError('Failed to initialize price manager')
        logger.info('Price manager initialized in %.1fs', time.time() - pm_start)
        self.calculator = ContractCalculator(self.database, self.price_manager)
        logger.info('Initialized TargetHuntingService OK')
        # Запускаем фоновый воркер сразу — он перезагрузит свежие цены из API
        # (т.к. _last_update_time не установлен при загрузке устаревшего кэша)
        # и пересчитает результаты без блокировки HTTP-запросов.
        self.start_refresher()

    def stop(self) -> None:
        self._stop.set()

    def _normalize_mode(self, mode: str) -> Tuple[str, str]:
        raw = str(mode or 'PROFIT').strip().upper()
        raw = raw.replace('_', '-').replace(' ', '')
        if raw in {'HIGH-RISK', 'HIGHRISK', 'RISK'}:
            return ('RISK', 'HIGH-RISK')
        return ('PROFIT', 'PROFIT')

    def _compute_results_part(self, *, calc_mode: str, is_stattrak: bool) -> List[Dict]:
        if not self.calculator:
            return []

        part_start = time.time()
        err: Optional[Exception] = None

        try:
            max_results = int(os.getenv('HUNT_MAX_RESULTS', '200') or 200)
        except Exception:
            max_results = 200

        try:
            min_roi_pct = float(os.getenv('HUNT_MIN_ROI_PCT', '2.0') or 2.0)
        except Exception:
            min_roi_pct = 2.0

        try:
            min_profit_probability = float(os.getenv('HUNT_MIN_PP', '0.25') or 0.25)
        except Exception:
            min_profit_probability = 0.25

        try:
            min_imbalance_ratio = float(os.getenv('HUNT_MIN_IMB', '1.2') or 1.2)
        except Exception:
            min_imbalance_ratio = 1.2

        try:
            max_targets_per_rarity = int(os.getenv('HUNT_MAX_TARGETS_PER_RARITY', '200') or 200)
        except Exception:
            max_targets_per_rarity = 200

        try:
            exploration_rate = float(os.getenv('HUNT_EXPLORATION_RATE', '0.15') or 0.15)
        except Exception:
            exploration_rate = 0.15

        rank_strategy = str(os.getenv('HUNT_RANK_STRATEGY', '') or '').strip()
        if not rank_strategy:
            rank_strategy = 'DEFAULT'

        try:
            min_cost = float(os.getenv('HUNT_MIN_COST', '0') or 0)
        except Exception:
            min_cost = 0.0

        try:
            min_net_profit = float(os.getenv('HUNT_MIN_NET_PROFIT', '0') or 0)
        except Exception:
            min_net_profit = 0.0

        try:
            part = self.calculator.find_target_hunting_pro_mode(
                max_results=max_results,
                max_investment=None,
                is_stattrak=is_stattrak,
                min_roi_pct=min_roi_pct,
                min_profit_probability=min_profit_probability,
                min_imbalance_ratio=min_imbalance_ratio,
                min_cost=min_cost,
                min_net_profit=min_net_profit,
                max_targets_per_rarity=max_targets_per_rarity,
                exploration_rate=exploration_rate,
                mode=calc_mode,
                rank_strategy=rank_strategy,
            )
        except Exception as e:
            err = e
            logger.exception('Mode=%s: computation failed (%s)', str(calc_mode), 'ST' if bool(is_stattrak) else 'NO')
            part = []

        part_list = list(part or [])
        logger.info(
            'Mode=%s: computed %s items (%s) in %.1fs',
            str(calc_mode),
            len(part_list),
            'ST' if bool(is_stattrak) else 'NO',
            time.time() - part_start,
        )

        if (not part_list) and err is None:
            logger.info(
                'Mode=%s: 0 items (%s). thresholds: HUNT_RANK_STRATEGY=%s HUNT_MIN_COST=%s HUNT_MIN_NET_PROFIT=%s HUNT_MIN_ROI_PCT=%s HUNT_MIN_PP=%s HUNT_MIN_IMB=%s HUNT_MAX_RESULTS=%s HUNT_MAX_TARGETS_PER_RARITY=%s HUNT_EXPLORATION_RATE=%s',
                str(calc_mode),
                'ST' if bool(is_stattrak) else 'NO',
                str(rank_strategy),
                str(min_cost),
                str(min_net_profit),
                str(min_roi_pct),
                str(min_profit_probability),
                str(min_imbalance_ratio),
                str(max_results),
                str(max_targets_per_rarity),
                str(exploration_rate),
            )

        if (not part_list) and err is None:
            try:
                pm = getattr(self, 'price_manager', None)
                mc = getattr(pm, 'market_client', None) if pm is not None else None
                if mc is not None:
                    with mc._prices_cache_lock:
                        cache_size = len(mc._prices_cache or {})
                        last_upd = float(getattr(mc, '_last_update_time', 0.0) or 0.0)
                    age_s = (time.time() - last_upd) if last_upd > 0 else None
                    valid = bool(mc._is_cache_valid()) if hasattr(mc, '_is_cache_valid') else None
                    logger.info(
                        'Mode=%s: 0 items (%s). price_cache_size=%s cache_age_s=%s cache_valid=%s',
                        str(calc_mode),
                        'ST' if bool(is_stattrak) else 'NO',
                        int(cache_size),
                        (f"{age_s:.1f}" if age_s is not None else 'N/A'),
                        str(valid),
                    )
            except Exception:
                logger.debug('Mode=%s: failed to collect 0-items diagnostics', str(calc_mode))
        return part_list

    def _compute_results_deep(self, *, calc_mode: str) -> List[Dict]:
        if not self.calculator:
            return []

        try:
            max_results = int(os.getenv('HUNT_MAX_RESULTS', '200') or 200)
        except Exception:
            max_results = 200

        results: List[Dict] = []
        results.extend(self._compute_results_part(calc_mode=calc_mode, is_stattrak=False))
        results.extend(self._compute_results_part(calc_mode=calc_mode, is_stattrak=True))

        if not results:
            return []

        def _sort_key(r: Dict) -> float:
            return float(
                r.get('_rank_score')
                or r.get('_base_rank_score')
                or r.get('final_score')
                or r.get('opportunity_score')
                or r.get('contract_score')
                or 0.0
            )

        results.sort(key=_sort_key, reverse=True)
        return results[: int(max_results)]

    def _apply_max_investment(self, results: List[Dict], max_investment: Optional[float]) -> List[Dict]:
        if max_investment is None:
            return list(results)
        out = []
        for r in results:
            try:
                ic = float(r.get('input_cost') or 0.0)
            except Exception:
                ic = 0.0
            if ic > 0.0 and ic <= float(max_investment):
                out.append(r)
        return out

    def get_cached(self, *, mode: str, max_investment: Optional[float], limit: int = 20) -> Tuple[List[Dict], Dict]:
        cache_mode, calc_mode = self._normalize_mode(mode)
        with self._cache_lock:
            entry = dict(self._cache.get(cache_mode) or {})

        # Cache is considered "ready" after at least one refresh attempt finished.
        # Results can legitimately be an empty list (no bundles found for current logic).
        try:
            ts = float(entry.get('timestamp') or 0.0)
        except Exception:
            ts = 0.0

        if ts <= 0.0:
            self.start_refresher()
            return ([], {
                'mode': cache_mode,
                'calc_mode': calc_mode,
                'ready': False,
                'refreshing': bool(entry.get('refreshing')),
                'timestamp': float(ts),
                'last_error': entry.get('last_error'),
            })

        filtered = self._apply_max_investment(list(entry.get('results') or []), max_investment)
        filtered = filtered[: int(limit)]
        meta = {
            'mode': cache_mode,
            'calc_mode': calc_mode,
            'ready': True,
            'refreshing': bool(entry.get('refreshing')),
            'timestamp': float(ts),
            'last_error': entry.get('last_error'),
        }
        self.start_refresher()
        return (filtered, meta)

    def refresh_mode(self, mode: str) -> None:
        cache_mode, calc_mode = self._normalize_mode(mode)
        with self._cache_lock:
            self._cache.setdefault(cache_mode, {
                'results': [],
                'timestamp': 0.0,
                'refreshing': False,
                'last_error': None,
            })
            if self._cache[cache_mode].get('refreshing'):
                return
            self._cache[cache_mode]['refreshing'] = True

        # Reset CSFloat session limits at the start of each refresh cycle so that
        # a quota exhaustion in the previous cycle does not permanently block the client.
        try:
            pm = getattr(self, 'price_manager', None)
            cfc = getattr(pm, 'csfloat_client', None) if pm is not None else None
            if cfc is not None and hasattr(cfc, 'reset_session_limits'):
                if getattr(cfc, '_session_disabled', False):
                    cfc.reset_session_limits()
        except Exception:
            pass

        try:
            start_ts = time.time()
            logger.info('Refreshing mode=%s (calc_mode=%s)...', cache_mode, calc_mode)

            try:
                max_results = int(os.getenv('HUNT_MAX_RESULTS', '200') or 200)
            except Exception:
                max_results = 200

            # Compute both normal and StatTrak before publishing to avoid UI inconsistency.
            updated_no = self._compute_results_part(calc_mode=calc_mode, is_stattrak=False)
            updated_st = self._compute_results_part(calc_mode=calc_mode, is_stattrak=True)
            updated = list(updated_no) + list(updated_st)

            # Sort/trim using the existing logic.
            if updated:
                def _sort_key(r: Dict) -> float:
                    return float(
                        r.get('_rank_score')
                        or r.get('_base_rank_score')
                        or r.get('final_score')
                        or r.get('opportunity_score')
                        or r.get('contract_score')
                        or 0.0
                    )

                updated.sort(key=_sort_key, reverse=True)
                updated = updated[: int(max_results)]

            # Precompute outcomes for top-N contracts before publishing.
            try:
                warm_n = int(os.getenv('OUTCOMES_WARMUP_TOPN') or 30)
            except Exception:
                warm_n = 30
            warm_n = max(0, min(int(warm_n), len(updated)))

            calc = getattr(self, 'calculator', None)
            def _precompute_for(r: Dict) -> None:
                if not (isinstance(r, dict) and calc is not None):
                    return
                try:
                    ins = list(r.get('input_skins') or [])
                    is_st = bool(r.get('is_stattrak'))
                except Exception:
                    ins = []
                    is_st = False
                if not ins:
                    r['outcomes'] = []
                    r['profit_probability'] = 0.0
                    return
                outs = calc.calculate_contract_outcomes_details(ins, is_stattrak=is_st)
                try:
                    outs = list(outs or [])
                    outs.sort(key=lambda x: float((x or {}).get('price') or 0.0), reverse=True)
                except Exception:
                    outs = []
                r['outcomes'] = outs
                try:
                    input_cost = float(r.get('input_cost') or 0.0)
                    pp = 0.0
                    for o in outs:
                        prob = float((o or {}).get('probability') or 0.0)
                        price = float((o or {}).get('price') or 0.0)
                        if price > float(input_cost) + 1e-12:
                            pp += prob
                    r['profit_probability'] = float(pp)
                except Exception:
                    pass

            if calc is not None and warm_n > 0:
                for r in updated[:warm_n]:
                    try:
                        _precompute_for(r)
                    except Exception:
                        pass

            # Publish only after full ST+non-ST and warmup is ready.
            publish_ts = time.time()
            dur = time.time() - start_ts
            with self._cache_lock:
                self._cache[cache_mode] = {
                    'results': list(updated),
                    'timestamp': float(publish_ts),
                    'refreshing': False,
                    'last_error': None,
                }

            # Warm remaining outcomes in background (does not block publication).
            try:
                if calc is not None and warm_n < len(updated):
                    remaining = list(updated[warm_n:])

                    def _bg_warm(rem: List[Dict], *, ts_snapshot: float) -> None:
                        for rr in rem:
                            try:
                                _precompute_for(rr)
                            except Exception:
                                continue
                        # Persist back into cache if snapshot still matches.
                        try:
                            with self._cache_lock:
                                cur = dict(self._cache.get(cache_mode) or {})
                                if bool(cur.get('refreshing')):
                                    return
                                cur_ts = float(cur.get('timestamp') or 0.0)
                                if abs(cur_ts - float(ts_snapshot)) > 1e-6:
                                    return
                                cur_results = list(cur.get('results') or [])
                                # Update by identity (same dict objects are expected, but be defensive).
                                self._cache[cache_mode] = {
                                    'results': cur_results,
                                    'timestamp': float(cur_ts),
                                    'refreshing': False,
                                    'last_error': cur.get('last_error'),
                                }
                        except Exception:
                            return

                    t = threading.Thread(target=_bg_warm, args=(remaining,), kwargs={'ts_snapshot': float(publish_ts)}, daemon=True)
                    t.start()
            except Exception:
                logger.debug('Failed to start background outcomes warmup', exc_info=True)

            logger.info('Refreshed mode=%s: %s items in %.1fs', cache_mode, len(updated), dur)
        except Exception as e:
            logger.exception('Failed refreshing mode=%s', cache_mode)
            with self._cache_lock:
                prev = dict(self._cache.get(cache_mode) or {})
                prev['refreshing'] = False
                prev['last_error'] = f"{type(e).__name__}: {e}"
                self._cache[cache_mode] = prev

    def get_contract_outcomes(self, contract: Dict, *, top_n: int = 5) -> List[Dict]:
        if not self.calculator:
            return []

        try:
            cached = contract.get('outcomes') if isinstance(contract, dict) else None
        except Exception:
            cached = None

        if isinstance(cached, list) and cached:
            try:
                outcomes = list(cached)
            except Exception:
                outcomes = []
        else:
            try:
                outcomes = self.calculator.calculate_contract_outcomes_details(
                    contract.get('input_skins') or [],
                    is_stattrak=bool(contract.get('is_stattrak')),
                )
            except Exception:
                outcomes = []
        try:
            outcomes = list(outcomes)
            outcomes.sort(key=lambda x: float(x.get('price') or 0.0), reverse=True)
        except Exception:
            outcomes = []
        try:
            n = int(top_n) if top_n is not None else 0
        except Exception:
            n = 0
        if n <= 0:
            return outcomes
        return outcomes[:n]

    def start_refresher(self) -> None:
        if self._refresh_thread and self._refresh_thread.is_alive():
            return

        def _worker():
            refresh_after_seconds = 60 * 60
            check_every_seconds = 60
            modes = ['PROFIT', 'RISK']

            while not self._stop.is_set():
                now = time.time()
                to_refresh = []

                with self._cache_lock:
                    for m in modes:
                        self._cache.setdefault(m, {
                            'results': [],
                            'timestamp': 0.0,
                            'refreshing': False,
                            'last_error': None,
                        })

                    for m in modes:
                        e = self._cache.get(m) or {}
                        try:
                            ts = float(e.get('timestamp') or 0.0)
                            refreshing = bool(e.get('refreshing'))
                        except Exception:
                            ts = 0.0
                            refreshing = False

                        if refreshing:
                            continue
                        if ts <= 0.0 or (now - ts) >= float(refresh_after_seconds):
                            to_refresh.append(m)

                if (not self._refresh_running.is_set()) and to_refresh:
                    self._refresh_running.set()
                    try:
                        # Update price cache before recomputing hunting results so $ profit values actually change.
                        try:
                            pm = getattr(self, 'price_manager', None)
                            if pm is not None:
                                ok = bool(pm.refresh_prices(force_refresh=False))
                                if ok:
                                    calc = getattr(self, 'calculator', None)
                                    if calc is not None and hasattr(calc, 'clear_price_memoization'):
                                        try:
                                            calc.clear_price_memoization()
                                        except Exception:
                                            logger.debug('Failed to clear calculator memoization after price refresh', exc_info=True)
                        except Exception:
                            logger.debug('Background price refresh failed', exc_info=True)

                        for m in to_refresh:
                            if self._stop.is_set():
                                break
                            self.refresh_mode(m)
                    finally:
                        self._refresh_running.clear()

                if self._stop.wait(check_every_seconds):
                    break

        self._refresh_thread = threading.Thread(target=_worker, daemon=True)
        self._refresh_thread.start()

    def cache_status(self) -> Dict[str, Dict]:
        with self._cache_lock:
            snap = {k: dict(v) for k, v in (self._cache or {}).items()}
        out = {}
        for k, v in snap.items():
            out[k] = {
                'timestamp': float(v.get('timestamp') or 0.0),
                'refreshing': bool(v.get('refreshing')),
                'count': len(v.get('results') or []),
                'last_error': v.get('last_error'),
            }
        return out
