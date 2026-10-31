import random

import pytest

from vistadb3.codec import read_delta32, read_delta64, write_delta32, write_delta64

# (previous value, new value, encoded delta) observed in files written by the engine
INT32_VECTORS = [
    (0, 1, "10"), (1, 0, "08"), (0, 2, "20"), (2, 0, "18"), (0, 15, "f0"), (15, 0, "e8"),
    (0, 16, "0101"), (16, 0, "f8"), (0, 17, "1101"), (17, 0, "0901"), (0, 255, "f10f"), (255, 0, "e90f"),
    (0, 256, "0110"), (256, 0, "f90f"), (0, 0x1000, "020001"), (0x1000, 0, "f9ff"), (0x10000, 0, "faff0f"),
    (0, -1, "08"), (-1, 0, "10"), (0, 0x12345678, "0478563412"), (0x12345678, 0, "0488a9cbed"),
    (0, 0x7FFFFFFF, "04ffffff7f"), (0x7FFFFFFF, 0, "0401000080"), (0, -0x80000000, "0400000080"),
]
INT64_VECTORS = [
    (1, 2, "20"), (2, 2, "00"), (2, 300, "4125"), (300, -5, "1126"), (-5, 268435456, "a400000002"),
    (268435456, 4294967296, "040000001e"), (4294967296, 9223372036854775807, "08fffffffffeffff7f"),
    (9223372036854775807, -9223372036854775807, "40"),
]


@pytest.mark.parametrize("prev,new,hexs", INT32_VECTORS)
def test_delta32_vectors(prev, new, hexs):
    assert write_delta32(new - prev).hex() == hexs
    d, pos = read_delta32(bytes.fromhex(hexs), 0)
    assert pos == len(hexs) // 2
    assert (prev + d + 2**31) % 2**32 - 2**31 == new


@pytest.mark.parametrize("prev,new,hexs", INT64_VECTORS)
def test_delta64_vectors(prev, new, hexs):
    delta = (new - prev + 2**63) % 2**64 - 2**63
    assert write_delta64(delta).hex() == hexs
    d, pos = read_delta64(bytes.fromhex(hexs), 0)
    assert pos == len(hexs) // 2
    assert d == delta


def test_delta_roundtrip_random():
    r = random.Random(3)
    for _ in range(20000):
        d32 = r.choice([r.randint(-2**31, 2**31 - 1), r.randint(-300, 300), r.randint(-2**20, 2**20)])
        assert read_delta32(write_delta32(d32), 0)[0] == d32
        d64 = r.choice([r.randint(-2**63, 2**63 - 1), r.randint(-300, 300), r.randint(-2**40, 2**40)])
        assert read_delta64(write_delta64(d64), 0)[0] == d64
