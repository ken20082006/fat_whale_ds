import re

import pytest

from dafeijing.core.access import (
    generate_code,
    hash_code,
    normalise_code,
)

CODE_PATTERN = re.compile(r"^DFJ-[A-Z2-9]{4}-[A-Z2-9]{4}$")


def test_generated_code_shape():
    for _ in range(50):
        assert CODE_PATTERN.match(generate_code())


def test_generated_codes_are_unique():
    codes = {generate_code() for _ in range(500)}
    assert len(codes) == 500


def test_ambiguous_characters_are_excluded():
    # I O 0 1 容易誤讀，不該出現在邀請碼裡
    joined = "".join(generate_code() for _ in range(200))
    body = joined.replace("DFJ", "").replace("-", "")
    assert not set(body) & set("IO01")


def test_hash_is_stable():
    assert hash_code("DFJ-7K2M-9QX4") == hash_code("dfj-7k2m-9qx4")


def test_hash_ignores_spaces():
    assert hash_code("DFJ 7K2M 9QX4") == hash_code("DFJ-7K2M-9QX4")


@pytest.mark.parametrize(
    "raw",
    [
        "DFJ-7K2M-9QX4",
        "dfj-7k2m-9qx4",
        "DFJ7K2M9QX4",
        "dfj 7k2m 9qx4",
        "DFJ_7K2M_9QX4",
    ],
)
def test_normalise_accepts_common_typing(raw):
    assert normalise_code(raw) == "DFJ-7K2M-9QX4"


def test_normalise_passes_through_garbage():
    # 長度不對就不硬掰，交給比對去判斷無效
    assert normalise_code("hello") == "HELLO"


def test_normalised_round_trip_hashes_match():
    assert hash_code(normalise_code("7k2m9qx4")) == hash_code("DFJ-7K2M-9QX4")
