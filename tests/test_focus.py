from omnivoice.windows.focus import FocusLease, leases_match


def test_focus_leases_match_all_identity_fields() -> None:
    lease = FocusLease((1, 2, 3), 10, 20, 50004)

    assert leases_match(lease, FocusLease((1, 2, 3), 10, 20, 50004))
    assert not leases_match(lease, FocusLease((1, 2, 4), 10, 20, 50004))
    assert not leases_match(lease, FocusLease((1, 2, 3), 11, 20, 50004))
    assert not leases_match(lease, FocusLease((1, 2, 3), 10, 21, 50004))
