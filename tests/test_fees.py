from src.fees import calculate_fee


class TestCalculateFee:
    def test_basic_fee(self):
        net, fee = calculate_fee(1_000_000, 10)
        assert fee == 1_000
        assert net == 999_000

    def test_zero_amount(self):
        net, fee = calculate_fee(0, 10)
        assert fee == 0
        assert net == 0

    def test_zero_bps(self):
        net, fee = calculate_fee(1_000_000, 0)
        assert fee == 0
        assert net == 1_000_000

    def test_truncation_on_small_amount(self):
        net, fee = calculate_fee(99, 10)
        assert fee == 0
        assert net == 99

    def test_100_bps_is_one_percent(self):
        net, fee = calculate_fee(10_000, 100)
        assert fee == 100
        assert net == 9_900

    def test_net_plus_fee_equals_gross(self):
        gross = 123_456_789
        net, fee = calculate_fee(gross, 25)
        assert net + fee == gross

    def test_wei_scale_amount(self):
        gross = 1_000_000_000_000_000_000
        net, fee = calculate_fee(gross, 10)
        assert fee == 1_000_000_000_000_000
        assert net == 999_000_000_000_000_000
