from app import security


def test_password_hash_roundtrip():
    stored = security.hash_password("correct horse")
    assert security._check_hash("correct horse", stored)
    assert not security._check_hash("wrong", stored)


def test_share_token_cannot_be_admin_session():
    share = security.issue_share("token123")
    assert security.verify_share("token123", share)
    assert not security.verify_share("other", share)
    # A share cookie must never double as an admin session cookie.
    assert not security.verify_session(share)


def test_session_tokens_verify():
    token = security.issue_session()
    assert security.verify_session(token)
    assert not security.verify_session(token + "x")


def test_range_parser():
    from app.http_range import RangeNotSatisfiable, parse_range

    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 10)
    assert parse_range("bytes=90-", 100) == (90, 10)
    assert parse_range("bytes=-10", 100) == (90, 10)
    for bad in ("bytes=200-300", "bytes=5-1", "bytes=-0", "items=0-1"):
        try:
            parse_range(bad, 100)
        except RangeNotSatisfiable:
            continue
        raise AssertionError(f"{bad} should be unsatisfiable")
