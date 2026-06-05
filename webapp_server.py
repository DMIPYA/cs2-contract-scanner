#!/usr/bin/env python3
"""
Telegram Mini App backend for Crafty CS2 Trade-Up Scanner.
Run this alongside telegram_bot.py.

Usage:
    python webapp_server.py

Requires WEBAPP_PORT in .env (default 8080).
The WEBAPP_URL in .env must point to this server (public HTTPS URL for Telegram).
"""

import os
import time
import json
import hmac
import hashlib
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name('.env'), override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('webapp_server')

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from bot_service import TargetHuntingService
from database import CS2Database


# ── Service singleton ────────────────────────────────────────────────────────

_svc_lock = threading.Lock()
_svc: Optional[TargetHuntingService] = None
_db: Optional[CS2Database] = None


def _get_svc() -> TargetHuntingService:
    global _svc, _db
    with _svc_lock:
        if _svc is None:
            logger.info('Initializing TargetHuntingService for webapp...')
            svc = TargetHuntingService()
            svc.initialize()
            _svc = svc
            _db = svc.calculator.database if hasattr(svc, 'calculator') else None
            logger.info('TargetHuntingService initialized.')
    return _svc


def _get_db() -> Optional[CS2Database]:
    """Get database instance from service"""
    _get_svc()  # Ensure service is initialized
    return _db


# ── FastAPI app ──────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _get_svc)
    yield

app = FastAPI(title='Crafty Mini App', docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['GET'],
    allow_headers=['*'],
)


# ── Helpers ──────────────────────────────────────────────────────────────────

_WEAR_ABBR = {
    'Factory New': 'FN',
    'Minimal Wear': 'MW',
    'Field-Tested': 'FT',
    'Well-Worn': 'WW',
    'Battle-Scarred': 'BS',
}

_RARITY_ORDER = ['Consumer', 'Industrial', 'Mil-Spec', 'Restricted', 'Classified', 'Covert']


def _normalize_mode(mode: str) -> str:
    raw = str(mode or 'PROFIT').strip().upper().replace('_', '-').replace(' ', '')
    if raw in {'SAFE'}:
        return 'SAFE'
    if raw in {'BID'}:
        return 'BID'
    return 'RISK' if raw in {'HIGH-RISK', 'HIGHRISK', 'RISK'} else 'PROFIT'


def _infer_rarity(c: dict) -> str:
    """Try to infer the INPUT skin rarity from whatever fields are available."""
    # Some calculator versions expose this directly
    for field in ('input_rarity', 'rarity', 'input_skin_rarity'):
        v = c.get(field)
        if v and str(v) in _RARITY_ORDER:
            return str(v)

    # Derive from input_skins if they carry a rarity field
    ins = list(c.get('input_skins') or [])
    if ins:
        skin = ins[0]
        for field in ('rarity', 'skin_rarity', 'grade'):
            v = skin.get(field)
            if v and str(v) in _RARITY_ORDER:
                return str(v)

    # Derive from output rarity: output is one tier above input
    out_rarity = c.get('output_rarity') or c.get('target_rarity')
    if out_rarity and str(out_rarity) in _RARITY_ORDER:
        idx = _RARITY_ORDER.index(str(out_rarity))
        if idx > 0:
            return _RARITY_ORDER[idx - 1]

    return 'Mil-Spec'  # safe default


def _serialize_contract_summary(idx: int, c: dict) -> dict:
    """Lightweight summary card for the list screen."""
    rarity = _infer_rarity(c)
    ins = list(c.get('input_skins') or [])
    # Collect unique wear abbreviations from inputs
    wears_seen: list = []
    for s in ins:
        w = str(s.get('wear') or '')
        abbr = _WEAR_ABBR.get(w)
        if abbr and abbr not in wears_seen:
            wears_seen.append(abbr)

    return {
        'idx': int(idx),
        'name': str(c.get('hunt_output') or ''),
        'collection': str(c.get('target_collection') or ''),
        'roi': round(float(c.get('roi') or 0.0), 2),
        'net_profit': round(float(c.get('net_profit') or 0.0), 2),
        'input_cost': round(float(c.get('input_cost') or 0.0), 2),
        'expected_output': round(float(c.get('expected_output') or 0.0), 2),
        'profit_probability': round(float(c.get('profit_probability') or 0.0) * 100.0, 1),
        'chance_of_target': round(float(c.get('chance_of_target') or 0.0) * 100.0, 1),
        'jackpot_ratio': round(float(c.get('jackpot_ratio') or 0.0), 2),
        'is_stattrak': bool(c.get('is_stattrak')),
        'rarity': rarity,
        'input_wears': wears_seen,
        'expected_wear': str(c.get('hunt_expected_wear') or ''),
        'target_wear': str(c.get('hunt_target_wear') or ''),
        'max_avg_float': round(float(c.get('target_max_avg_float') or 0.0), 4) if c.get('target_max_avg_float') else None,
    }


def _serialize_contract_detail(idx: int, c: dict, *, mode: str = 'PROFIT') -> dict:
    """Full detail for the detail screen."""
    summary = _serialize_contract_summary(idx, c)

    # Resolve service and price manager once for the whole function
    _svc_inst = _get_svc()
    _pm = getattr(_svc_inst, 'price_manager', None)

    ins = list(c.get('input_skins') or [])
    
    # Calculate max allowed average float for inputs based on target wear
    # This shows the maximum average float that keeps the target skin quality
    max_allowed_avg_float = None
    max_allowed_wear = None
    
    target_wear = c.get('hunt_target_wear') or c.get('expected_wear')
    target_skin_name = c.get('hunt_output') or c.get('name')
    
    db = _get_db()
    if target_wear and target_skin_name and db:
        # Wear boundaries (upper limit for each quality, exclusive)
        # FN: [0.00, 0.07), MW: [0.07, 0.15), FT: [0.15, 0.38), WW: [0.38, 0.45), BS: [0.45, 1.00]
        wear_upper_limits = {
            'Factory New': 0.07,
            'Minimal Wear': 0.15,
            'Field-Tested': 0.38,
            'Well-Worn': 0.45,
            'Battle-Scarred': 1.00,
        }
        
        # Get the upper boundary for target wear (exclusive, so we use slightly less)
        target_boundary = wear_upper_limits.get(target_wear, 0.07)
        # Use a small epsilon to ensure we stay below the boundary
        if target_wear != 'Battle-Scarred':
            target_boundary = target_boundary - 0.0001
        
        # Get target skin data to understand its float range
        target_skin = db.get_skin_by_name(target_skin_name)
        if target_skin:
            target_min = float(target_skin.min_float)
            target_max = float(target_skin.max_float)
            
            # Clamp target_boundary to skin's actual range
            target_boundary = min(target_boundary, target_max)
            target_boundary = max(target_boundary, target_min)
            
            # The output float formula: out_float = avg_norm * (target_max - target_min) + target_min
            # We need: out_float < target_boundary (strictly less for non-BS)
            # So: avg_norm * (target_max - target_min) + target_min < target_boundary
            # avg_norm < (target_boundary - target_min) / (target_max - target_min)
            
            if target_max > target_min:
                max_avg_norm = (target_boundary - target_min) / (target_max - target_min)
                max_avg_norm = min(1.0, max(0.0, max_avg_norm))
                
                # Now denormalize this for input skins to get real float values
                if ins:
                    denorm_floats = []
                    for s in ins:
                        skin_name = s.get('name', '')
                        if skin_name:
                            skin_data = db.get_skin_by_name(skin_name)
                            if skin_data:
                                min_f = float(skin_data.min_float)
                                max_f = float(skin_data.max_float)
                                # Denormalize: real_float = norm * (max - min) + min
                                denorm_float = max(min_f, min(max_f, max_avg_norm))
                                denorm_floats.append(denorm_float)
                    
                    if denorm_floats:
                        max_allowed_avg_float = round(sum(denorm_floats) / len(denorm_floats), 4)

                        # Determine wear quality based on max allowed float
                        if max_allowed_avg_float < 0.07:
                            max_allowed_wear = 'Factory New'
                        elif max_allowed_avg_float < 0.15:
                            max_allowed_wear = 'Minimal Wear'
                        elif max_allowed_avg_float < 0.38:
                            max_allowed_wear = 'Field-Tested'
                        elif max_allowed_avg_float < 0.45:
                            max_allowed_wear = 'Well-Worn'
                        else:
                            max_allowed_wear = 'Battle-Scarred'

    # Group inputs by (name, wear, buy_source, collection)
    # Skins with same name/wear/source are merged regardless of individual float values.
    # Float is shown as average across the group.
    groups: OrderedDict = OrderedDict()
    for s in ins:
        nm = str(s.get('name') or '')
        wr = str(s.get('wear') or '')
        coll = str(s.get('collection') or '')
        src = str(s.get('buy_source') or 'MARKETCSGO')
        fl = s.get('float')
        fl_val = float(fl) if fl is not None else None
        key = (nm, wr, src, coll)
        g = groups.get(key)
        if g is None:
            g = {
                'name': nm,
                'wear': wr,
                'wear_abbr': _WEAR_ABBR.get(wr, wr[:2].upper()),
                'collection': coll,
                'count': 0,
                'total_price': 0.0,
                'floats': [],
                'max_float_for_wear': s.get('max_float_for_wear'),
                'buy_source': src,
                'individual_skins': [],
            }
            groups[key] = g
        g['count'] += 1
        price = 0.0
        try:
            price = float(s.get('price') or 0.0)
            g['total_price'] += price
        except Exception:
            pass
        
        if fl_val is not None:
            g['floats'].append(fl_val)
            
        g['individual_skins'].append({
            'float': fl_val,
            'price': price,
        })

    _WEAR_MAX_FLOAT = {
        'Factory New':   0.0699,
        'Minimal Wear':  0.1499,
        'Field-Tested':  0.3799,
        'Well-Worn':     0.4499,
        'Battle-Scarred': 1.0,
    }

    input_groups = []
    pos = 1
    for g in groups.values():
        start_i = pos
        end_i = pos + g['count'] - 1
        pos = end_i + 1
        avg_float = None
        if g['floats']:
            avg_float = round(sum(g['floats']) / len(g['floats']), 4)
        per_item = round(g['total_price'] / max(g['count'], 1), 2)

        # max_float_for_wear: from optimization if available, else wear boundary
        mf = g.get('max_float_for_wear')
        if mf is None:
            mf = _WEAR_MAX_FLOAT.get(g['wear'])

        # Request price suggestion (best-effort) — skip in BID mode (price IS the bid)
        request_price = None
        if mode != 'BID':
            try:
                if _pm is not None and g['buy_source'] != 'CSFLOAT':
                    _req = _pm.suggest_request_price(
                        g['name'],
                        target_wear=g['wear'] or None,
                        require_stattrak=bool(c.get('is_stattrak')),
                        buy_source=g['buy_source'],
                    )
                    if _req and _req.get('suggested_price') is not None:
                        request_price = round(float(_req['suggested_price']), 2)
            except Exception:
                pass

        # Market depth: how many lots available at/near the listed price
        # Shows avg_price_for_count to warn when not enough cheap lots exist
        market_depth = None
        try:
            if _pm is not None and g['buy_source'] in ('MARKETCSGO', 'MARKETCSGO_BID', ''):
                _mc = _pm.market_client
                _prices = _mc.get_real_listings(
                    g['name'],
                    target_wear=g['wear'] or None,
                    exclude_stattrak=not bool(c.get('is_stattrak')),
                    require_stattrak=bool(c.get('is_stattrak')),
                    limit=g['count'] + 5,
                )
                if _prices:
                    available = len(_prices)
                    needed = g['count']
                    prices_for_needed = _prices[:needed]
                    avg_for_needed = round(sum(prices_for_needed) / len(prices_for_needed), 2) if prices_for_needed else None
                    market_depth = {
                        'available': available,
                        'needed': needed,
                        'avg_price_for_needed': avg_for_needed,
                        'sufficient': available >= needed,
                    }
        except Exception:
            pass

        input_groups.append({
            'start': start_i,
            'end': end_i,
            'name': g['name'],
            'wear': g['wear'],
            'wear_abbr': g['wear_abbr'],
            'collection': g['collection'],
            'count': g['count'],
            'total_price': round(g['total_price'], 2),
            'per_item': per_item,
            'avg_float': avg_float,
            'max_float_for_wear': round(mf, 4) if mf is not None else None,
            'buy_source': g['buy_source'],
            'individual_skins': g['individual_skins'],
            'request_price': request_price,
            'market_depth': market_depth,
        })

    # Outcomes
    raw_outs = list(c.get('outcomes') or [])
    outcomes = []
    for o in raw_outs:
        wr = str(o.get('wear') or '')
        sell_src = str(o.get('sell_source') or 'MARKETCSGO')

        # Instant-sell price (best bid on the market)
        instant_sell = None
        try:
            if _pm is not None:
                _sr = _pm.suggest_sell_price(
                    str(o.get('name') or ''),
                    target_wear=wr or None,
                    require_stattrak=bool(c.get('is_stattrak')),
                    sell_source=sell_src,
                )
                if _sr and _sr.get('instant_sell') is not None:
                    instant_sell = round(float(_sr['instant_sell']), 2)
        except Exception:
            pass

        outcomes.append({
            'name': str(o.get('name') or ''),
            'price': round(float(o.get('price') or 0.0), 2),
            'probability': round(float(o.get('probability') or 0.0) * 100.0, 2),
            'wear': wr,
            'wear_abbr': _WEAR_ABBR.get(wr, wr[:2].upper() if wr else '?'),
            'sell_source': sell_src,
            'instant_sell': instant_sell,
        })

    core = f"{int(c.get('main_skins_count') or 0)}/{int(c.get('filler_skins_count') or 0)}"

    # Recalculate input_cost using real market depth avg prices where available
    real_input_cost = 0.0
    has_real_cost = False
    for g in input_groups:
        md = g.get('market_depth')
        if md and md.get('avg_price_for_needed') is not None:
            real_input_cost += round(float(md['avg_price_for_needed']) * int(g['count']), 2)
            has_real_cost = True
        else:
            real_input_cost += float(g.get('total_price') or 0.0)

    if has_real_cost:
        real_input_cost = round(real_input_cost, 2)
        ev = float(c.get('expected_output') or 0.0)
        real_net_profit = round(ev - real_input_cost, 2)
        real_roi = round((ev - real_input_cost) / real_input_cost * 100.0, 2) if real_input_cost > 0 else 0.0
    else:
        real_input_cost = float(c.get('input_cost') or 0.0)
        real_net_profit = float(c.get('net_profit') or 0.0)
        real_roi = float(c.get('roi') or 0.0)

    detail = dict(summary)
    detail.update({
        'core_filler': core,
        'input_groups': input_groups,
        'outcomes': outcomes,
        'total_inputs': len(ins),
        'max_allowed_avg_float': max_allowed_avg_float,
        'max_allowed_wear': max_allowed_wear,
        'bid_mode': mode == 'BID',
        # Override with real market depth costs
        'input_cost': real_input_cost,
        'net_profit': real_net_profit,
        'roi': real_roi,
    })
    return detail


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
async def serve_app() -> HTMLResponse:
    # Ищем в webapp/index.html или просто в index.html
    current_dir = Path(__file__).resolve().parent
    html_paths = [
        current_dir / 'webapp' / 'index.html',
        current_dir / 'index.html',
    ]
    
    for p in html_paths:
        if p.exists():
            return HTMLResponse(content=p.read_text(encoding='utf-8'))
            
    # Если не нашли, выведем список файлов для отладки прямо в браузере
    files = [str(f.name) for f in current_dir.iterdir()]
    webapp_files = []
    if (current_dir / 'webapp').is_dir():
        webapp_files = [str(f.name) for f in (current_dir / 'webapp').iterdir()]
        
    error_msg = f"HTML not found. Files in root: {files}. Files in webapp: {webapp_files}"
    logger.error(error_msg)
    raise HTTPException(status_code=404, detail=error_msg)


@app.get('/api/contracts')
async def api_contracts(
    mode: str = Query('PROFIT'),
    limit: int = Query(50, ge=1, le=200),
    rarity: str = Query('all'),
) -> dict:
    mode = _normalize_mode(mode)
    try:
        svc = _get_svc()
    except Exception as e:
        logger.exception('Service init failed')
        raise HTTPException(status_code=503, detail='Service unavailable')

    results, meta = svc.get_cached(mode=mode, max_investment=None, limit=200)

    if not meta.get('ready'):
        return {
            'ready': False,
            'refreshing': bool(meta.get('refreshing')),
            'contracts': [],
            'total': 0,
            'mode': mode,
        }

    # Sort by net_profit for PROFIT mode (matches bot behaviour)
    if mode == 'PROFIT' and results:
        try:
            results = sorted(results, key=lambda x: float(x.get('net_profit') or 0.0), reverse=True)
        except Exception:
            pass
    # Sort SAFE and BID modes by ROI descending
    if mode in ('SAFE', 'BID') and results:
        try:
            results = sorted(results, key=lambda x: float(x.get('roi') or 0.0), reverse=True)
        except Exception:
            pass

    # Rarity filter
    if rarity and rarity.lower() not in ('all', ''):
        rarity_filter = rarity.strip()
        results = [r for r in results if _infer_rarity(r).lower() == rarity_filter.lower()]

    total = len(results)
    results = results[:limit]

    contracts = []
    for i, c in enumerate(results, start=1):
        try:
            contracts.append(_serialize_contract_summary(i, c))
        except Exception:
            logger.debug('Failed to serialize contract %d', i, exc_info=True)

    ts = float(meta.get('timestamp') or 0.0)
    age_min = round((time.time() - ts) / 60.0, 1) if ts > 0 else None
    from datetime import datetime
    last_updated = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts > 0 else None

    return {
        'ready': True,
        'refreshing': bool(meta.get('refreshing')),
        'contracts': contracts,
        'total': total,
        'mode': mode,
        'cache_age_min': age_min,
        'last_updated': last_updated,
        'timestamp': ts,
    }


@app.get('/api/contract/{idx}')
async def api_contract_detail(
    idx: int,
    mode: str = Query('PROFIT'),
) -> dict:
    mode = _normalize_mode(mode)
    if idx < 1 or idx > 500:
        raise HTTPException(status_code=400, detail='Invalid index')

    try:
        svc = _get_svc()
    except Exception as e:
        logger.exception('Service init failed')
        raise HTTPException(status_code=503, detail='Service unavailable')

    results, meta = svc.get_cached(mode=mode, max_investment=None, limit=200)

    if not meta.get('ready'):
        raise HTTPException(status_code=503, detail='Cache not ready yet')

    if mode == 'PROFIT' and results:
        try:
            results = sorted(results, key=lambda x: float(x.get('net_profit') or 0.0), reverse=True)
        except Exception:
            pass
    if mode in ('SAFE', 'BID') and results:
        try:
            results = sorted(results, key=lambda x: float(x.get('roi') or 0.0), reverse=True)
        except Exception:
            pass

    if idx > len(results):
        raise HTTPException(status_code=404, detail='Contract not found')

    c = results[idx - 1]

    # Ensure outcomes are computed (they may not be ready for items beyond warmup_n)
    if not c.get('outcomes'):
        try:
            calc = getattr(svc, 'calculator', None)
            if calc is not None:
                ins = list(c.get('input_skins') or [])
                is_st = bool(c.get('is_stattrak'))
                if ins:
                    outs = calc.calculate_contract_outcomes_details(ins, is_stattrak=is_st)
                    outs = sorted(outs or [], key=lambda x: float(x.get('price') or 0.0), reverse=True)
                    c['outcomes'] = outs
        except Exception:
            logger.debug('Failed to compute outcomes for idx=%d', idx, exc_info=True)

    return _serialize_contract_detail(idx, c, mode=mode)


@app.get('/api/status')
async def api_status() -> dict:
    try:
        svc = _get_svc()
        st = svc.cache_status()
        return {'ok': True, 'cache': st}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ── Favorites storage ────────────────────────────────────────────────────────

_FAVORITES_DIR = Path(os.getenv('FAVORITES_DIR', '/app/favorites') if os.path.exists('/app') else 'favorites')
_favorites_lock = threading.Lock()


def _get_favorites_path(user_id: str) -> Path:
    _FAVORITES_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = str(user_id).replace('/', '').replace('..', '').strip()[:32]
    return _FAVORITES_DIR / f'fav_{safe_id}.json'


def _verify_telegram_init_data(init_data: str) -> Optional[str]:
    """
    Verify Telegram WebApp initData and return user_id if valid.
    Returns None if invalid or bot token not set.
    """
    bot_token = str(os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
    if not bot_token or not init_data:
        return None
    try:
        import urllib.parse
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_val = parsed.pop('hash', '')
        if not hash_val:
            return None
        # Build data-check-string
        data_check = '\n'.join(f'{k}={v}' for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, hash_val):
            return None
        # Extract user_id
        user_str = parsed.get('user', '{}')
        user = json.loads(user_str)
        uid = str(user.get('id') or '')
        return uid if uid else None
    except Exception:
        return None


@app.get('/api/favorites')
async def api_favorites_get(request: Request) -> dict:
    """Get favorites for the current Telegram user."""
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    user_id = _verify_telegram_init_data(init_data)
    if not user_id:
        # Dev fallback: allow without auth if no bot token configured
        user_id = request.headers.get('X-Dev-User-Id', '')
        if not user_id:
            raise HTTPException(status_code=401, detail='Unauthorized')

    path = _get_favorites_path(user_id)
    with _favorites_lock:
        if path.exists():
            try:
                favs = json.loads(path.read_text(encoding='utf-8'))
                return {'ok': True, 'favorites': favs if isinstance(favs, list) else []}
            except Exception:
                pass
    return {'ok': True, 'favorites': []}


@app.post('/api/favorites')
async def api_favorites_save(request: Request) -> dict:
    """Save favorites for the current Telegram user."""
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    user_id = _verify_telegram_init_data(init_data)
    if not user_id:
        user_id = request.headers.get('X-Dev-User-Id', '')
        if not user_id:
            raise HTTPException(status_code=401, detail='Unauthorized')

    try:
        body = await request.json()
        favs = body.get('favorites', [])
        if not isinstance(favs, list):
            raise HTTPException(status_code=400, detail='favorites must be a list')
        # Limit size
        favs = favs[:200]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    path = _get_favorites_path(user_id)
    with _favorites_lock:
        path.write_text(json.dumps(favs, ensure_ascii=False), encoding='utf-8')

    return {'ok': True, 'saved': len(favs)}


@app.post('/api/refresh')
async def api_refresh() -> dict:
    """Force refresh of price cache and contract calculations"""
    try:
        svc = _get_svc()
        logger.info('Mini App: Force refresh requested')
        svc.refresh_background()
        return {'ok': True, 'message': 'Refresh started'}
    except Exception as e:
        logger.exception('Mini App refresh failed')
        return {'ok': False, 'error': str(e)}


@app.get('/api/contract/find')
async def api_contract_find(
    collection: str = Query(''),
    output: str = Query(''),
    is_stattrak: bool = Query(False),
    mode: str = Query('PROFIT'),
) -> dict:
    """
    Find a contract by its unique key (collection, output skin, is_stattrak).
    Returns full detail if found, or {'found': False} if not in current cache.
    Used by Favorites to check if a saved contract is still available.
    """
    mode = _normalize_mode(mode)
    if not collection or not output:
        return {'found': False}

    try:
        svc = _get_svc()
    except Exception:
        return {'found': False}

    results, meta = svc.get_cached(mode=mode, max_investment=None, limit=200)
    if not meta.get('ready') or not results:
        return {'found': False}

    # Sort same as list endpoint
    if mode == 'PROFIT':
        try:
            results = sorted(results, key=lambda x: float(x.get('net_profit') or 0.0), reverse=True)
        except Exception:
            pass
    elif mode in ('SAFE', 'BID'):
        try:
            results = sorted(results, key=lambda x: float(x.get('roi') or 0.0), reverse=True)
        except Exception:
            pass

    # Find by unique key
    col_lower = collection.strip().lower()
    out_lower = output.strip().lower()
    for i, c in enumerate(results, start=1):
        c_col = str(c.get('target_collection') or '').strip().lower()
        c_out = str(c.get('hunt_output') or '').strip().lower()
        c_st = bool(c.get('is_stattrak'))
        if c_col == col_lower and c_out == out_lower and c_st == bool(is_stattrak):
            # Ensure outcomes computed
            if not c.get('outcomes'):
                try:
                    calc = getattr(svc, 'calculator', None)
                    if calc is not None:
                        ins = list(c.get('input_skins') or [])
                        if ins:
                            outs = calc.calculate_contract_outcomes_details(ins, is_stattrak=c_st)
                            c['outcomes'] = sorted(outs or [], key=lambda x: float(x.get('price') or 0.0), reverse=True)
                except Exception:
                    pass
            detail = _serialize_contract_detail(i, c, mode=mode)
            detail['found'] = True
            detail['current_idx'] = i
            return detail

    return {'found': False}


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.getenv('WEBAPP_PORT') or 8080)
    host = str(os.getenv('WEBAPP_HOST') or '0.0.0.0')
    logger.info('Starting Crafty Mini App server on %s:%d', host, port)
    uvicorn.run(app, host=host, port=port, log_level='info')
