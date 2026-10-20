#!/usr/bin/env python3
"""给 redroid 11 的 libndk_translation.so 打 MprotectForGuest 单字节补丁。

背景：libndk 在 MprotectForGuest 里用 ``and edx, 0xfffffffa`` 同时清掉了 PROT_READ
和 PROT_EXEC，再 ``or edx, 1`` 补回 PROT_READ。明日方舟 2.7.81 起会 mprotect ART
boot image 所在页，执行位被清掉后，下次跳进那页就 SIGSEGV(SEGV_ACCERR) 闪退
（日志里是 CrashSight "get signal 11"，然后画面冻结在加载页）。把掩码改成
0xfffffffe 只清 PROT_READ，保留调用方请求的执行权限。
参考：https://blog.chilldove.com/posts/redroid11-arknights-ndk-translation-mprotect/

只依赖标准库。按 ELF 符号定位函数、在函数体内找唯一匹配后才写，已打过补丁则直接返回。

用法: patch_libndk.py <输入.so> <输出.so>
退出码: 0 已打补丁/已是补丁版; 2 找不到补丁点(版本不同,请勿强行修改)。
"""
from __future__ import annotations

import struct
import sys

SYMBOL = b"_ZN15ndk_translation16MprotectForGuestEPvmi"
ORIGINAL = bytes.fromhex("83e2fa83ca01")  # and edx,0xfffffffa ; or edx,1
PATCHED = bytes.fromhex("83e2fe83ca01")  # and edx,0xfffffffe ; or edx,1
SEARCH_WINDOW = 0x200


def _sections(data: bytes):
    shoff = struct.unpack_from("<Q", data, 0x28)[0]
    shentsize, shnum, _shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    for index in range(shnum):
        base = shoff + index * shentsize
        name, sh_type, _flags, addr, offset, size, link, _info, _align, entsize = struct.unpack_from(
            "<IIQQQQIIQQ", data, base
        )
        yield index, sh_type, addr, offset, size, link, entsize


def find_symbol_value(data: bytes, symbol: bytes) -> int:
    sections = list(_sections(data))
    for _index, sh_type, _addr, offset, size, link, entsize in sections:
        if sh_type not in (2, 11) or not entsize:  # SHT_SYMTAB / SHT_DYNSYM
            continue
        strtab_offset = sections[link][3]
        for pos in range(offset, offset + size, entsize):
            st_name, _info, _other, _shndx, st_value, _st_size = struct.unpack_from(
                "<IBBHQQ", data, pos
            )
            end = data.index(b"\0", strtab_offset + st_name)
            if data[strtab_offset + st_name : end] == symbol:
                return st_value
    raise LookupError("symbol not found")


def vaddr_to_offset(data: bytes, vaddr: int) -> int:
    phoff = struct.unpack_from("<Q", data, 0x20)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    for index in range(phnum):
        p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _align = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phentsize
        )
        if p_type == 1 and p_vaddr <= vaddr < p_vaddr + p_filesz:
            return vaddr - p_vaddr + p_offset
    raise LookupError("address not in any PT_LOAD segment")


def patch(data: bytes) -> tuple[bytes, str]:
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[18] != 0x3E:
        raise ValueError("不是 x86_64 ELF64 文件")
    start = vaddr_to_offset(data, find_symbol_value(data, SYMBOL))
    window = data[start : start + SEARCH_WINDOW]
    if window.count(PATCHED) == 1 and window.count(ORIGINAL) == 0:
        return data, "already-patched"
    if window.count(ORIGINAL) != 1:
        raise LookupError("MprotectForGuest 中没有唯一的补丁点")
    at = start + window.index(ORIGINAL)
    return data[:at] + PATCHED + data[at + len(PATCHED) :], f"patched@0x{at:x}"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 64
    with open(argv[1], "rb") as file:
        data = file.read()
    try:
        patched, status = patch(data)
    except (LookupError, ValueError) as exc:
        print(f"[patch-libndk] 跳过：{exc}", file=sys.stderr)
        return 2
    with open(argv[2], "wb") as file:
        file.write(patched)
    print(f"[patch-libndk] {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
