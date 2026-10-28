# coding: utf-8
"""兼容旧入口；实际工作由每日涨停池脚本完成。"""
from sync_limit_up import main

if __name__ == "__main__":
    raise SystemExit(main())
