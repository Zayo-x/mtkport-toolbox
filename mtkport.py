# -*- coding: utf-8 -*-
r"""MTK 系统移植工具箱 —— 入口。

用法：
  python mtkport.py info   boot.img
  python mtkport.py unpack boot.img -o 工作目录\boot
  python mtkport.py patch  boot.img --rules configs\p100-lineage14.json -o 新boot.img
  python mtkport.py sys-extract system.img -o 工作目录\system
  python mtkport.py sys-pack    工作目录\system -o system-new.img --size 0x32000000
  python mtkport.py scatter-flash scatter.txt --only BOOTIMG --images BOOTIMG=新boot.img -o 刷机文件夹
  python mtkport.py -h

需要的外部程序（自动在这些位置找，也可以用环境变量 MTKPORT_BIN 指目录）：
  7z            解 system.img
  make_ext4fs   打 system.img
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mtkport.cli import main   # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
