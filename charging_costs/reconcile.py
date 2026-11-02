from datetime import datetime
from decimal import Decimal


def preview(
    bill,
    sessions,
    *,
    currency='USD',
    tolerance_seconds=300,
):
    if not isinstance(bill, dict):
        raise ValueError('bill must be a dict')
    if not isinstance(sessions, list):
        raise ValueError('sessions must be a list')

    required_bill_fields = ('external_id', 'vin', 'started_at', 'amount', 'currency', 'finalized')
    for field in required_bill_fields:
        if field not in bill:
            raise ValueError(f'missing bill field: {field}')

    external_id = bill['external_id']
    if not isinstance(external_id, str) or external_id.strip() == '':
        raise ValueError('bill.external_id must be a nonblank string')

    bill_vin = bill['vin']
    if not isinstance(bill_vin, str) or bill_vin.strip() == '':
        raise ValueError('bill.vin must be a nonblank string')

    bill_started_at = bill['started_at']
    if not isinstance(bill_started_at, datetime) or bill_started_at.utcoffset() is None:
        raise ValueError('bill.started_at must be an aware datetime')

    bill_amount = bill['amount']
    if isinstance(bill_amount, bool) or not isinstance(bill_amount, Decimal):
        raise ValueError('bill.amount must be a Decimal')
    if not bill_amount.is_finite() or bill_amount < 0:
        raise ValueError('bill.amount must be finite and nonnegative')

    bill_currency = bill['currency']
    if not isinstance(bill_currency, str) or bill_currency.strip() == '':
        raise ValueError('bill.currency must be a nonblank string')

    finalized = bill['finalized']
    if not isinstance(finalized, bool):
        raise ValueError('bill.finalized must be a bool')

    expected_currency = currency
    if not isinstance(expected_currency, str) or expected_currency.strip() == '':
        raise ValueError('currency must be a nonblank string')

    if isinstance(tolerance_seconds, bool) or not isinstance(tolerance_seconds, (int, float)):
        raise ValueError('tolerance_seconds must be a non-bool number')
    try:
        tolerance_seconds_decimal = Decimal(tolerance_seconds)
    except Exception as exc:
        raise ValueError('tolerance_seconds must be a finite nonnegative number') from exc
    if not tolerance_seconds_decimal.is_finite() or tolerance_seconds_decimal < 0:
        raise ValueError('tolerance_seconds must be a finite nonnegative number')

    if len(sessions) > 1000:
        raise ValueError('sessions must contain at most 1000 items')

    seen_session_ids = set()
    for index, session in enumerate(sessions):
        if not isinstance(session, dict):
            raise ValueError(f'session[{index}] must be a dict')

        required_session_fields = ('id', 'vin', 'started_at', 'cost')
        for field in required_session_fields:
            if field not in session:
                raise ValueError(f'missing session field: {field}')

        session_id = session['id']
        if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 0:
            raise ValueError(f'session[{index}].id must be a positive integer')
        if session_id in seen_session_ids:
            raise ValueError(f'duplicate session id: {session_id}')
        seen_session_ids.add(session_id)

        session_vin = session['vin']
        if not isinstance(session_vin, str) or session_vin.strip() == '':
            raise ValueError(f'session[{index}].vin must be a nonblank string')

        session_started_at = session['started_at']
        if not isinstance(session_started_at, datetime) or session_started_at.utcoffset() is None:
            raise ValueError(f'session[{index}].started_at must be an aware datetime')

        session_cost = session['cost']
        if session_cost is not None:
            if isinstance(session_cost, bool) or not isinstance(session_cost, Decimal):
                raise ValueError(f'session[{index}].cost must be None or a Decimal')
            if not session_cost.is_finite() or session_cost < 0:
                raise ValueError(f'session[{index}].cost must be finite and nonnegative')

    def result(status, session_id=None, amount=None):
        return {'status': status, 'session_id': session_id, 'amount': amount}

    if not finalized:
        return result('pending')

    if bill_currency != expected_currency:
        return result('currency_mismatch')

    matching_sessions = []
    protected_sessions = []
    for session in sessions:
        if session['vin'] != bill_vin:
            continue

        delta_seconds = abs(
            Decimal(
                (session['started_at'] - bill_started_at).total_seconds()
            )
        )
        if delta_seconds <= tolerance_seconds_decimal:
            matching_sessions.append(session)
            if session['cost'] is not None:
                protected_sessions.append(session)

    if protected_sessions:
        return result('protected_existing')

    if not matching_sessions:
        return result('no_match')

    if len(matching_sessions) > 1:
        return result('ambiguous')

    match = matching_sessions[0]
    return result('proposed', match['id'], bill_amount)
