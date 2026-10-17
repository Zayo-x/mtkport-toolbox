# -*- coding: utf-8 -*-
"""mtkport —— MTK 系统移植工具箱。

模块划分：
  util      日志/md5/找外部程序/跑子进程
  cpio      ramdisk 的 cpio(newc) 读写
  bootimg   boot.img 解析、解包、原地换 gzip、重排重打包
  rules     声明式补丁规则引擎（prop/replace/regex/append/add/delete/chmod）
  systemimg system.img 解包(sparse 也能解)、fs_config 生成、make_ext4fs 打包、树合并
  scatter   MTK scatter 分区表读写、只刷某几个分区、整盘 dump 偏移核对
  cli       命令行界面
"""
__version__ = '1.0'
