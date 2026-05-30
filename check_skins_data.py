#!/usr/bin/env python3
"""
Скрипт для проверки качества данных в skins.json
Проверяет заполненность полей wears, min_float, max_float
"""

import json
import sys
from collections import defaultdict

def check_skins_data():
    """Проверка данных скинов"""
    
    try:
        with open('skins.json', 'r', encoding='utf-8') as f:
            skins_data = json.load(f)
    except Exception as e:
        print(f"❌ Ошибка чтения skins.json: {e}")
        return
    
    print(f"📊 Всего скинов в файле: {len(skins_data)}")
    print("=" * 80)
    
    # Статистика
    stats = {
        'total': 0,
        'weapons_only': 0,
        'has_wears': 0,
        'empty_wears': 0,
        'has_min_float': 0,
        'has_max_float': 0,
        'invalid_float_range': 0,
        'wears_count': defaultdict(int),
    }
    
    # Примеры проблемных скинов
    examples_no_wears = []
    examples_invalid_float = []
    
    for skin in skins_data:
        stats['total'] += 1
        
        # Проверяем, что это оружие
        category = skin.get("category", {})
        category_name = category.get("name", "").lower() if category else ""
        
        excluded_categories = [
            "gloves", "stickers", "patches", "charms", "graffiti", 
            "musical kits", "agents", "keys", "cases", "tools"
        ]
        
        if any(excluded in category_name for excluded in excluded_categories):
            continue
            
        weapon_data = skin.get("weapon", {})
        if not weapon_data or not weapon_data.get("name"):
            continue
        
        stats['weapons_only'] += 1
        
        # Проверяем wears
        wears = skin.get('wears', [])
        if wears and isinstance(wears, list) and len(wears) > 0:
            stats['has_wears'] += 1
            stats['wears_count'][len(wears)] += 1
        else:
            stats['empty_wears'] += 1
            if len(examples_no_wears) < 5:
                examples_no_wears.append({
                    'name': skin.get('name', 'Unknown'),
                    'weapon': weapon_data.get('name', 'Unknown'),
                    'rarity': skin.get('rarity', {}).get('name', 'Unknown'),
                })
        
        # Проверяем float
        min_float = skin.get('min_float')
        max_float = skin.get('max_float')
        
        if min_float is not None:
            stats['has_min_float'] += 1
        if max_float is not None:
            stats['has_max_float'] += 1
        
        # Проверяем корректность диапазона
        if min_float is not None and max_float is not None:
            if min_float < 0 or max_float > 1 or min_float >= max_float:
                stats['invalid_float_range'] += 1
                if len(examples_invalid_float) < 5:
                    examples_invalid_float.append({
                        'name': skin.get('name', 'Unknown'),
                        'min_float': min_float,
                        'max_float': max_float,
                    })
    
    # Вывод статистики
    print("\n📈 СТАТИСТИКА ПО ОРУЖИЮ:")
    print(f"  Всего оружия: {stats['weapons_only']}")
    print(f"  С заполненным wears: {stats['has_wears']} ({stats['has_wears']/max(1,stats['weapons_only'])*100:.1f}%)")
    print(f"  С пустым wears: {stats['empty_wears']} ({stats['empty_wears']/max(1,stats['weapons_only'])*100:.1f}%)")
    print(f"  С min_float: {stats['has_min_float']} ({stats['has_min_float']/max(1,stats['weapons_only'])*100:.1f}%)")
    print(f"  С max_float: {stats['has_max_float']} ({stats['has_max_float']/max(1,stats['weapons_only'])*100:.1f}%)")
    print(f"  С некорректным диапазоном float: {stats['invalid_float_range']}")
    
    print("\n📊 РАСПРЕДЕЛЕНИЕ КОЛИЧЕСТВА WEARS:")
    for count in sorted(stats['wears_count'].keys()):
        num = stats['wears_count'][count]
        print(f"  {count} wears: {num} скинов ({num/max(1,stats['has_wears'])*100:.1f}%)")
    
    # Примеры проблемных скинов
    if examples_no_wears:
        print("\n⚠️  ПРИМЕРЫ СКИНОВ БЕЗ WEARS:")
        for ex in examples_no_wears:
            print(f"  - {ex['weapon']} | {ex['name']} ({ex['rarity']})")
    
    if examples_invalid_float:
        print("\n❌ ПРИМЕРЫ СКИНОВ С НЕКОРРЕКТНЫМ FLOAT:")
        for ex in examples_invalid_float:
            print(f"  - {ex['name']}: min={ex['min_float']}, max={ex['max_float']}")
    
    # Проверяем конкретные скины из Revolution Collection
    print("\n🔍 ПРОВЕРКА REVOLUTION COLLECTION:")
    revolution_skins = []
    for skin in skins_data:
        weapon_data = skin.get("weapon", {})
        if not weapon_data or not weapon_data.get("name"):
            continue
        
        # Ищем скины, которые могут быть из Revolution
        name = skin.get('name', '')
        if 'Sakkaku' in name or 'Anubis' in name or 'Viper' in name:
            wears = skin.get('wears', [])
            wears_names = [w.get('name') for w in wears if isinstance(w, dict)] if isinstance(wears, list) else []
            revolution_skins.append({
                'name': name,
                'weapon': weapon_data.get('name', ''),
                'min_float': skin.get('min_float'),
                'max_float': skin.get('max_float'),
                'wears': wears_names,
                'wears_count': len(wears_names),
            })
    
    if revolution_skins:
        for skin in revolution_skins[:10]:
            print(f"\n  {skin['weapon']} | {skin['name']}")
            print(f"    Float: {skin['min_float']:.4f} - {skin['max_float']:.4f}")
            print(f"    Wears ({skin['wears_count']}): {', '.join(skin['wears']) if skin['wears'] else 'ПУСТО'}")
    else:
        print("  Скины не найдены (возможно, нужно проверить collections.json)")
    
    print("\n" + "=" * 80)
    
    # Выводы
    print("\n💡 ВЫВОДЫ:")
    if stats['empty_wears'] > stats['weapons_only'] * 0.1:
        print(f"  ⚠️  КРИТИЧНО: {stats['empty_wears']/max(1,stats['weapons_only'])*100:.1f}% скинов без wears!")
        print("     Это объясняет проблему с качеством в боте.")
        print("     Бот предполагает все качества доступны, но база не содержит эту информацию.")
    else:
        print(f"  ✅ Большинство скинов имеют wears ({stats['has_wears']/max(1,stats['weapons_only'])*100:.1f}%)")
    
    if stats['invalid_float_range'] > 0:
        print(f"  ⚠️  Найдено {stats['invalid_float_range']} скинов с некорректным float диапазоном")
    
    return stats

if __name__ == '__main__':
    check_skins_data()