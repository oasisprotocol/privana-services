def calculate_fee(gross_amount: int, fee_bps: int) -> tuple[int, int]:
    fee = (gross_amount * fee_bps) // 10_000
    net = gross_amount - fee
    return net, fee
