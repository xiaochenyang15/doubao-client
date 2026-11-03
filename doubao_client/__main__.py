"""支持 ``python -m doubao_client`` 启动。

PyInstaller 以**脚本**方式加载入口，此时没有包上下文（``__package__`` 为空），
相对导入会失败；因此这种情况下退回绝对导入。
"""
if __package__:
    from .app import main
else:                                   # 直接执行本文件 / 冻结后的入口脚本
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from doubao_client.app import main

if __name__ == "__main__":
    raise SystemExit(main())
