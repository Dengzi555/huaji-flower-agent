"""审计时间的唯一来源：带时区的 UTC 时间戳，与业务发生日期严格分开。

各模块的审计字段（录入、导入、事件、持久化更新时间）统一取本模块的当前时间，
避免同一语义在多个模块各写一份 datetime.now 实现后格式悄悄分叉。

本模块只产生“现在”，不产生业务日期：数据日期、目标日期、对比基线日一律由
输入或文件提供，不得用当前时间冒充业务发生时间。
"""

from datetime import datetime, timezone


def utc_now():
    """返回带时区偏移的 UTC 时间字符串；持久化统一使用这个格式。"""
    return datetime.now(timezone.utc).isoformat()
