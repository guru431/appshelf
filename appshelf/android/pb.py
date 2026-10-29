"""Protobuf без зависимостей — ровно столько, сколько нужно клиенту Google Play (android/play.py): запись полей по
номерам и разбор в {номер: [значения]}. Схем нет: номера полей — в play.py, по заметкам живой проверки."""
from __future__ import annotations


class PbError(ValueError):
    """Байты — не protobuf: обрезано, неизвестный тип поля, путь ведёт не в сообщение."""


def varint(n: int) -> bytes:
    if not 0 <= n < 1 << 64:
        raise PbError(f"число вне 0…2**64: {n}")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode(fields: list[tuple[int, object]]) -> bytes:
    """int/bool → varint, bytes/str → length-delimited, list — вложенное сообщение; None — поля нет."""
    out = bytearray()
    for num, val in fields:
        if val is None:
            continue
        if isinstance(val, (bool, int)):
            out += varint(num << 3) + varint(int(val))
            continue
        data = val.encode() if isinstance(val, str) else val if isinstance(val, bytes) else encode(val)
        out += varint(num << 3 | 2) + varint(len(data)) + data
    return bytes(out)


def _varint(data: bytes, p: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        if p >= len(data) or shift > 63:
            raise PbError("обрезано")
        b = data[p]
        p += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, p


def _skip(data: bytes, p: int, wt: int, num: int) -> int:
    """Конец значения с типом wt, начатого с p; группа — до своего конца."""
    if wt == 0:
        return _varint(data, p)[1]
    if wt == 1:
        q = p + 8
    elif wt == 5:
        q = p + 4
    elif wt == 2:
        n, p = _varint(data, p)
        q = p + n
    elif wt == 3:
        while True:
            key, p = _varint(data, p)
            if key >> 3 == 0:
                raise PbError("поле 0")
            if key & 7 == 4:
                if key >> 3 != num:
                    raise PbError("чужой конец группы")
                return p
            p = _skip(data, p, key & 7, key >> 3)
    else:
        raise PbError(f"неизвестный тип поля {wt}")
    if q > len(data):
        raise PbError("обрезано")
    return q


def decode(data: bytes) -> dict[int, list]:
    """{номер: [значения]}: varint, fixed64 и fixed32 — int, length-delimited — bytes; группы пропускаются."""
    out: dict[int, list] = {}
    p = 0
    while p < len(data):
        key, p = _varint(data, p)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise PbError("поле 0")
        if wt == 4:
            raise PbError("конец группы без начала")
        q = _skip(data, p, wt, num)
        if wt == 0:
            val = _varint(data, p)[0]
        elif wt == 2:
            val = _payload(data, p)
        elif wt in (1, 5):
            val = int.from_bytes(data[p:q], "little")
        else:
            p = q
            continue
        out.setdefault(num, []).append(val)
        p = q
    return out


def _payload(data: bytes, p: int) -> bytes:
    n, p = _varint(data, p)
    return data[p:p + n]


def path(data: bytes, *nums: int) -> list:
    """Все значения последнего поля по пути: на каждом шаге — первое вложенное сообщение; нет поля — []."""
    msg = decode(data)
    for num in nums[:-1]:
        vals = msg.get(num)
        if not vals:
            return []
        if not isinstance(vals[0], bytes):
            raise PbError(f"поле {num} — не сообщение")
        msg = decode(vals[0])
    return msg.get(nums[-1], [])
