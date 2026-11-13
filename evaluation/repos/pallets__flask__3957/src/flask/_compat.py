import asyncio
import functools
import inspect
import sys


def iscoroutinefunction(func):
    # Python < 3.8 does not correctly determine partially wrapped
    # coroutine functions are coroutine functions, hence the need for
    # this to exist. Code taken from CPython.
    if sys.version_info >= (3, 8):
        return asyncio.iscoroutinefunction(func)
    else:
        while inspect.ismethod(func):
            func = func.__func__
        while isinstance(func, functools.partial):
            func = func.func

        return asyncio.iscoroutinefunction(func)
