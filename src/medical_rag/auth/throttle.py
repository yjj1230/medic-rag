"""密码登录端点的暴力破解防护。

``/v1/auth/login`` 是系统中唯一的凭据入口，它以手机号加密码鉴权 —— 这是一对低熵
组合，以机器速度做猜测是很现实的。它此前接受无限次尝试，既无延迟也无记录。

这里的防护刻意做得小而自洽：

* 按标识符**且**按来源地址的滑动失败窗口，因此单个账号和单台主机都不会被猛攻；
* 恒定开销的失败路径（见 ``auth.routes`` —— 账号不存在时仍会执行一次 bcrypt 比较），
  因此响应内容和耗时都不会泄漏账号是否存在；
* 内存有界：该表在写入时做裁剪并设硬上限，因此攻击者无法把限流器本身变成一次
  无界内存 DoS。

范围说明：这是按进程的状态。它能挡住常见的单节点撞库模式，并且诚实地承认其局限 ——
多副本部署应改为由共享缓存（Redis）承载，本项目已经在跑 Redis。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class _AttemptWindow:
    """某个限流键的近期失败时间戳。"""

    failures: list[float] = field(default_factory=list)

    def prune(self, *, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        if self.failures and self.failures[0] < cutoff:
            self.failures = [stamp for stamp in self.failures if stamp >= cutoff]


class LoginThrottle:
    """按标识符和按客户端地址分别计数的滑动窗口失败计数器。

    两个键都必须低于限制，登录才会被尝试。同时统计客户端地址和标识符，正是为了
    阻止单台主机遍历一长串手机号 —— 而仅按标识符限流是拦不住的。
    """

    # 被跟踪键数的硬上限。达到上限会淘汰最旧的条目，因此无论
    # 攻击者发来多少不同的标识符，内存都是有界的。
    MAX_TRACKED_KEYS = 10_000

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: float = 300.0,
        clock=time.monotonic,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._windows: OrderedDict[str, _AttemptWindow] = OrderedDict()

    def retry_after(self, *keys: str) -> float:
        """距离被限制最严的键重新放行还有多少秒（无限制时为 0）。"""
        now = self._clock()
        longest = 0.0
        for key in keys:
            window = self._windows.get(key)
            if window is None:
                continue
            window.prune(now=now, window_seconds=self.window_seconds)
            if len(window.failures) < self.max_failures:
                continue
            oldest = window.failures[-self.max_failures]
            longest = max(longest, self.window_seconds - (now - oldest))
        return max(0.0, longest)

    def record_failure(self, *keys: str) -> None:
        now = self._clock()
        for key in keys:
            window = self._windows.get(key)
            if window is None:
                window = _AttemptWindow()
                self._windows[key] = window
            window.prune(now=now, window_seconds=self.window_seconds)
            window.failures.append(now)
            self._windows.move_to_end(key)
        self._evict_overflow()

    def record_success(self, *keys: str) -> None:
        """密码正确时，清空它所证明掌控的那些键的计数器。"""
        for key in keys:
            self._windows.pop(key, None)

    def _evict_overflow(self) -> None:
        while len(self._windows) > self.MAX_TRACKED_KEYS:
            self._windows.popitem(last=False)


def throttle_key_pair(identifier: str, client_host: str | None) -> tuple[str, str]:
    """一次尝试对应的限流键：账号与来源主机。

    标识符会被规范化，因此 ``13800000000`` 与 `` 13800000000 `` 共用一个计数器，
    而不会因为空白字符让攻击者白捡几次尝试机会。
    """
    return (f"id:{identifier.strip().lower()}", f"ip:{client_host or 'unknown'}")
