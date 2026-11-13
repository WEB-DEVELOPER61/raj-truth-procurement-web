SCHEMA_COLS = [
    'tender_id', 'nit_no', 'division', 'district', 'tender_amount',
    'estimated_cost_lac', 'actual_window_days', 'statutory_min_days',
    'deficit_days', 'is_violation', 'risk_score', 'publication_date'
]

def parse_dates_and_window(pub_str, close_str):
    """Returns (ISO publication_date or '', notice duration in days)."""
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            d1 = datetime.strptime(pub_str.strip(), fmt)
            d2 = datetime.strptime(close_str.strip(), fmt)
            return d1.strftime("%Y-%m-%d"), max(1, (d2 - d1).days)
        except ValueError:
            continue
    return "", 10

def calculate_rule_43_compliance(tender_amount, actual_window_days):
    """Exact match with app.py calculate_rule_43()."""
    if tender_amount > 20000000.0:
        statutory_min = 30
    elif tender_amount > 5000000.0:
        statutory_min = 15
    elif tender_amount > 1000000.0:
        statutory_min = 10
    else:
        statutory_min = 7

    is_violation = actual_window_days < statutory_min
    deficit = max(0.0, float(statutory_min - actual_window_days))

    if is_violation:
        val_weight = min(30.0, (np.log10(max(1000.0, float(tender_amount))) / 8.0) * 30.0)
        dur_weight = min(70.0, (deficit / statutory_min) * 70.0)
        risk_score = round(min(100.0, val_weight + dur_weight), 1)
    else:
        risk_score = 0.0

    return statutory_min, deficit, is_violation, risk_score
