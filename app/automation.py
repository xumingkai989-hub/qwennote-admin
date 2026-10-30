"""Validated, persistent automation preferences (separate from process secrets)."""
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AutomationSettings:
    sync_enabled: bool = True
    sync_time: str = '03:00'
    sync_interval_hours: int = 0
    backfill_days: int = 90
    tag_enabled: bool = True
    tag_time: str = '05:00'
    retention_days: int = 0
    sync_mode_default: str = 'quick'
    full_sync_enabled: bool = True
    full_sync_weekday: int = 6
    full_sync_time: str = '03:30'

    def __post_init__(self):
        if self.sync_mode_default not in ('quick', 'full'):
            raise ValueError('每日模式须为 quick 或 full。')
        if type(self.full_sync_weekday) is not int or not 0 <= self.full_sync_weekday <= 6:
            raise ValueError('星期须为 0–6 的整数。')
        for name in ('sync_enabled', 'tag_enabled', 'full_sync_enabled'):
            if type(getattr(self, name)) is not bool:
                raise ValueError('启用状态必须为布尔值。')
        for name in ('sync_time', 'tag_time', 'full_sync_time'):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
                raise ValueError('时间须为 HH:MM（00:00–23:59）。')
        for name, minimum, label in (
            ('sync_interval_hours', 0, '同步间隔'), ('backfill_days', 1, '补拉天数'),
            ('retention_days', 0, '保留天数'),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= 999999:
                raise ValueError(f'{label}须为 {minimum}–999999 的整数。')

    def values(self):
        return asdict(self)


def parse_settings_form(fields):
    def integer(name):
        text = fields.get(name, [''])[0]
        if not re.fullmatch(r'[0-9]{1,6}', text):
            raise ValueError('天数和间隔须为非负整数；补拉天数至少为 1。')
        return int(text)

    for name in ('sync_enabled', 'tag_enabled', 'full_sync_enabled'):
        if name in fields and fields[name][0] != 'true':
            raise ValueError('启用状态无效。')
    return AutomationSettings(
        sync_enabled='sync_enabled' in fields,
        sync_time=fields.get('sync_time', [''])[0],
        sync_interval_hours=integer('sync_interval_hours'),
        backfill_days=integer('backfill_days'),
        tag_enabled='tag_enabled' in fields,
        tag_time=fields.get('tag_time', [''])[0],
        retention_days=integer('retention_days'),
        sync_mode_default=fields.get('sync_mode_default', ['quick'])[0],
        full_sync_enabled='full_sync_enabled' in fields,
        full_sync_weekday=int(fields.get('full_sync_weekday', ['6'])[0])
            if re.fullmatch(r'[0-6]', fields.get('full_sync_weekday', ['6'])[0]) else -1,
        full_sync_time=fields.get('full_sync_time', ['03:30'])[0],
    )
