"""Compatibility stubs for retired UI media harvesting.

Media is read locally or captured by the process observer. No screenshot,
vision model, mouse, keyboard, navigation or scrolling is used here.
"""


def harvest(display_name=None, nav=60, log=print):
    log('已使用本地媒体与进程捕获，停用界面翻图')
    return 0


def pull_latest_fullres(display_name, log=print):
    return False


def _harvest_locked(display_name, nav, log):
    return 0
