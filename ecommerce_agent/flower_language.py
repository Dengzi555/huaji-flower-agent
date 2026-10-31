"""花店口语归一：小词表做可解释纠正，保留原话，不做无边界模糊猜测。"""

import re


def normalize_flower_query(text):
    """返回规范问题及命中规则；只纠正已登记的花材简称和特定错字短语。"""
    rules = [
        (r"乌梅包括", "乌梅玫瑰抱抱桶", "花店错字词表：乌梅包括→乌梅玫瑰抱抱桶"),
        (r"红玫(?!瑰)", "红玫瑰", "花材简称：红玫→红玫瑰"),
        (r"紫玫(?!瑰)", "紫玫瑰", "花材简称：紫玫→紫玫瑰"),
        (r"乌梅(?!玫瑰)", "乌梅玫瑰", "花材简称：乌梅→乌梅玫瑰"),
        (r"红色玫瑰", "红玫瑰", "颜色同义表达：红色玫瑰→红玫瑰"),
        (r"粉色康乃馨", "粉康乃馨", "颜色同义表达：粉色康乃馨→粉康乃馨"),
    ]
    changes = []
    for pattern, replacement, reason in rules:
        text, count = re.subn(pattern, replacement, text)
        if count:
            changes.append(reason)
    # 只在明确花材前补朵数单位，不把日期、价格、订单号的数字当规格。
    pattern = r"(?<!\d)(\d{1,4})(?=红玫瑰|乌梅玫瑰|紫玫瑰|向日葵|洋桔梗|粉康乃馨)"
    text, count = re.subn(pattern, r"\1朵", text)
    if count:
        changes.append("规格省略：花材前的数量补全为朵数")
    return text, changes
