"""局部商品条件修改：保留未被用户改动的品种/朵数/类型，缺规格时先确认。"""
import re


def product_patch(question, catalog, base_sku):
    """返回修改后的结构化约束及候选；不把放宽约束的建议自动视为确认。"""
    product=catalog.products.get(base_sku)
    if not product:return None
    identity=catalog.resolve(question)
    # 完整SKU、四字花名或品种表达属于换商品，交原实体解析器处理。
    if identity['matched_by'] in ('sku','flower_name'):return None
    if identity['matched_by']=='catalog_alias' and any(p.get('variety')!=product.get('variety') for p in identity['candidates']):return None
    counts={int(n) for n in re.findall(r'(?<!\d)(\d{1,4})(?:朵|枝)',question)}
    kinds={kind for kind in ('花束','抱抱桶','花篮','花盒') if kind in question}
    colors=set(re.findall(r'(白|红|粉|紫|黄|蓝|混)色',question))
    if not counts and not kinds and not colors:return None
    proposed={'stem_count':product.get('stem_count'),'variety':product.get('variety'),'product_type':product.get('product_type')}
    changed=[]
    if len(counts)>1 or len(kinds)>1 or len(colors)>1:
        return {'constraints':proposed,'candidates':[],'exact':False,'changed':[],'message':'一次请明确一种朵数、颜色和类型。'}
    if counts:proposed['stem_count']=next(iter(counts));changed.append('stem_count')
    if kinds:proposed['product_type']=next(iter(kinds));changed.append('product_type')
    if colors:
        old=proposed['variety'] or ''
        color=next(iter(colors));color='混色' if color=='混' else color
        proposed['variety']=re.sub(r'^(?:白|红|粉|紫|黄|蓝|混色|香槟)',color,old) if re.match(r'白|红|粉|紫|黄|蓝|混色|香槟',old) else color+old
        changed.append('variety')
    all_products=list(catalog.products.values())
    exact=[p for p in all_products if all(p.get(k)==v for k,v in proposed.items())]
    # 没有精确规格时，建议只放宽未修改字段，且必须明确确认；不跨品种凑一个SKU。
    alternatives=[p for p in all_products if p.get('variety')==proposed['variety'] and all(p.get(k)==proposed[k] for k in changed)]
    label=f"{proposed['stem_count']}朵{proposed['variety']}{proposed['product_type']}"
    return {'constraints':proposed,'candidates':exact or alternatives[:8],'exact':bool(exact),'changed':changed,
        'message':(f'目录中没有{label}。下方是不同规格的候选，确认前不会改用其他朵数或类型。' if alternatives else f'目录中没有{label}，也没有符合修改条件的同品种候选，请修改规格。') if not exact else '已仅修改明确提出的商品条件。'}
