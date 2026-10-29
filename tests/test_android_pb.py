import pytest

from appshelf.android import pb


def key(num: int, wt: int) -> bytes:
    return pb.varint(num << 3 | wt)


def test_pb_roundtrip_nested_and_varint():
    data = pb.encode([(1, 150), (2, "тест"), (3, [(1, 2**63 - 1), (2, b"\x00\xff")]), (3, [(1, 0)]), (4, True),
                      (5, None), (6, b"")])
    d = pb.decode(data)
    assert d[1] == [150] and d[2] == ["тест".encode()] and d[4] == [1] and d[6] == [b""] and 5 not in d
    assert len(d[3]) == 2 and pb.decode(d[3][1]) == {1: [0]}
    assert pb.path(data, 3, 1) == [2**63 - 1]               # промежуточное — первое из повторов
    assert pb.path(data, 3, 2) == [b"\x00\xff"] and pb.path(data, 3) == d[3]
    assert pb.path(data, 9) == [] and pb.path(data, 3, 9) == [] and pb.path(data, 9, 1) == []
    for bad in (-1, 2**64):                                  # отрицательных и больше 64 бит нет
        with pytest.raises(pb.PbError):
            pb.encode([(1, bad)])
    raw = (key(1, 1) + (7).to_bytes(8, "little") + key(2, 5) + (9).to_bytes(4, "little")
           + key(3, 3) + key(1, 0) + pb.varint(5) + key(3, 4) + key(4, 0) + pb.varint(1))
    assert pb.decode(raw) == {1: [7], 2: [9], 4: [1]}       # fixed64, fixed32; группа пропущена


@pytest.mark.parametrize("raw", [
    b"\x08\x96",                         # varint не закончен
    key(2, 2) + pb.varint(5) + b"ab",    # длина за концом
    key(1, 1) + b"\x00" * 7,             # fixed64 короче 8
    key(1, 6) + b"\x00",                 # неизвестный тип
    key(0, 0) + b"\x01",                 # поле 0
    key(3, 3) + key(1, 0) + b"\x01",     # группа не закрыта
    key(3, 4),                           # конец группы без начала
])
def test_pb_truncated_raises(raw):
    with pytest.raises(pb.PbError):
        pb.decode(raw)
    with pytest.raises(pb.PbError):
        pb.path(pb.encode([(1, 5)]), 1, 2)                    # путь через число, а не сообщение
