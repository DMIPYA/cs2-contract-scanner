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
import logging
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name('.env'), override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('webapp_server')

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from bot_service import TargetHuntingService


# ── Service singleton ────────────────────────────────────────────────────────

_svc_lock = threading.Lock()
_svc: Optional[TargetHuntingService] = None


def _get_svc() -> TargetHuntingService:
    global _svc
    with _svc_lock:
        if _svc is None:
            logger.info('Initializing TargetHuntingService for webapp...')
            svc = TargetHuntingService()
            svc.initialize()
            _svc = svc
            logger.info('TargetHuntingService initialized.')
    return _svc


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title='Crafty Mini App', docs_url=None, redoc_url=None)

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
    }


def _serialize_contract_detail(idx: int, c: dict) -> dict:
    """Full detail for the detail screen."""
    summary = _serialize_contract_summary(idx, c)

    ins = list(c.get('input_skins') or [])
    # Group inputs by (name, wear, collection)
    from collections import OrderedDict
    groups: OrderedDict = OrderedDict()
    for s in ins:
        nm = str(s.get('name') or '')
        wr = str(s.get('wear') or '')
        coll = str(s.get('collection') or '')
        key = (nm, wr, coll)
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
            }
            groups[key] = g
        g['count'] += 1
        try:
            g['total_price'] += float(s.get('price') or 0.0)
        except Exception:
            pass
        try:
            fl = s.get('float')
            if fl is not None:
                g['floats'].append(float(fl))
        except Exception:
            pass

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
        })

    # Outcomes
    raw_outs = list(c.get('outcomes') or [])
    outcomes = []
    for o in raw_outs:
        wr = str(o.get('wear') or '')
        outcomes.append({
            'name': str(o.get('name') or ''),
            'price': round(float(o.get('price') or 0.0), 2),
            'probability': round(float(o.get('probability') or 0.0) * 100.0, 2),
            'wear': wr,
            'wear_abbr': _WEAR_ABBR.get(wr, wr[:2].upper() if wr else '?'),
        })

    core = f"{int(c.get('main_skins_count') or 0)}/{int(c.get('filler_skins_count') or 0)}"

    detail = dict(summary)
    detail.update({
        'core_filler': core,
        'input_groups': input_groups,
        'outcomes': outcomes,
        'total_inputs': len(ins),
    })
    return detail


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
async def serve_app() -> HTMLResponse:
    html_path = Path(__file__).resolve().parent / 'webapp' / 'index.html'
    if not html_path.exists():
        raise HTTPException(status_code=404, detail='Mini App HTML not found')
    return HTMLResponse(content=html_path.read_text(encoding='utf-8'))


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

    return {
        'ready': True,
        'refreshing': bool(meta.get('refreshing')),
        'contracts': contracts,
        'total': total,
        'mode': mode,
        'cache_age_min': age_min,
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

    if idx > len(results):
        raise HTTPException(status_code=404, detail='Contract not found')

    c = results[idx - 1]

    # Ensure outcomes are computed (they may not be ready for items beyond warmup_n)
    if not c.get('outcomes'):
        calc = getattr(svc, 'calculator', None)
        if calc is not None:
            try:
                ins = list(c.get('input_skins') or [])
                is_st = bool(c.get('is_stattrak'))
                if ins:
                    outs = calc.calculate_contract_outcomes_details(ins, is_stattrak=is_st)
                    outs = sorted(outs or [], key=lambda x: float(x.get('price') or 0.0), reverse=True)
                    c['outcomes'] = outs
            except Exception:
                logger.debug('Failed to compute outcomes for idx=%d', idx, exc_info=True)

    return _serialize_contract_detail(idx, c)


@app.get('/api/status')
async def api_status() -> dict:
    try:
        svc = _get_svc()
        st = svc.cache_status()
        return {'ok': True, 'cache': st}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.getenv('WEBAPP_PORT') or 8080)
    host = str(os.getenv('WEBAPP_HOST') or '0.0.0.0')
    logger.info('Starting Crafty Mini App server on %s:%d', host, port)
    uvicorn.run(app, host=host, port=port, log_level='info')
