"""Pure, fail-closed Tesla Fleet billing preview. Never contacts Tesla or a DB."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def history_records(history):
    if isinstance(history, dict):
        history = history.get('response', history)
        if isinstance(history, dict):
            history = history.get('data')
    if not isinstance(history, list) or len(history) > 1000:
        raise ValueError('history must contain a data list of at most 1000 bills')
    return history


def timestamp(value, *, db=False):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime):
        raise ValueError('missing timestamp')
    if value.tzinfo is None:
        if not db:
            raise ValueError('billing timestamp needs timezone')
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError('missing number')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid number') from exc
    if not result.is_finite() or result < 0:
        raise ValueError('number must be finite and nonnegative')
    return result


def normalize_bill(bill):
    if not isinstance(bill, dict):
        raise ValueError('bill must be an object')
    if type(bill.get('sessionId')) is not int or bill['sessionId'] < 0:
        raise ValueError('invalid billing session ID')
    if not isinstance(bill.get('vin'), str) or not bill['vin'].strip():
        raise ValueError('missing VIN')
    start = timestamp(bill.get('chargeStartDateTime'))
    end = timestamp(bill.get('chargeStopDateTime'))
    if end < start:
        raise ValueError('end precedes start')
    fees = bill.get('fees')
    if not isinstance(fees, list) or not fees:
        raise ValueError('missing fees')
    def unsupported(value):
        return any(word in str(value).upper() for word in ('CREDIT', 'REFUND', 'REVERS'))

    if unsupported(bill.get('billingType')):
        raise ValueError('credits/refunds unsupported')
    invoices = bill.get('invoices')
    pending = not isinstance(invoices, list) or not invoices
    for invoice in invoices if isinstance(invoices, list) else []:
        if not isinstance(invoice, dict) or unsupported(invoice.get('invoiceType')):
            raise ValueError('unsupported invoice')
        if not all(isinstance(invoice.get(key), str) and invoice[key].strip()
                   for key in ('fileName', 'contentId', 'invoiceType')):
            pending = True
    amount = Decimal(0)
    energy = Decimal(0)
    charging_seen = False
    fee_ids = set()
    suffixes = ('Base', 'Tier1', 'Tier2', 'Tier3', 'Tier4')
    for fee in fees:
        if not isinstance(fee, dict):
            raise ValueError('invalid fee')
        fee_id = fee.get('sessionFeeId')
        if type(fee_id) not in (int, str) or not str(fee_id) or str(fee_id) in fee_ids:
            raise ValueError('missing/duplicate fee ID')
        fee_ids.add(str(fee_id))
        kind = fee.get('feeType')
        if kind not in ('CHARGING', 'CONGESTION') or fee.get('currencyCode') != 'USD':
            raise ValueError('unsupported fee or currency')
        if unsupported(fee.get('status')) or unsupported(fee.get('pricingType')):
            raise ValueError('credits/refunds unsupported')
        pending |= fee.get('isPaid') is not True or fee.get('status') != 'PAID'
        due = number(fee.get('totalDue'))
        components = []
        for key in ('totalDue', 'netDue') + tuple('total' + s for s in suffixes):
            if key in fee and fee[key] is not None:
                money = number(fee[key])
                if money != money.quantize(Decimal('0.01')):
                    raise ValueError('fractional cents')
                if key.startswith('total') and key != 'totalDue':
                    components.append(money)
        if components and sum(components, Decimal(0)) != due:
            raise ValueError('inconsistent fee totals')
        amount += due
        usage = [number(fee['usage' + s]) for s in suffixes if fee.get('usage' + s) is not None]
        for s in suffixes:
            if fee.get('rate' + s) is not None:
                number(fee['rate' + s])
        if kind == 'CHARGING':
            if str(fee.get('uom', '')).lower() != 'kwh' or not usage:
                raise ValueError('charging requires metered kWh')
            charging_seen = True
            energy += sum(usage, Decimal(0))
        elif str(fee.get('uom', '')).lower() != 'min':
            raise ValueError('unsupported congestion unit')
    if not charging_seen:
        raise ValueError('missing charging energy')
    if bill.get('unlatchDateTime') is not None and timestamp(bill['unlatchDateTime']) < end:
        raise ValueError('unlatch precedes charging end')
    site = bill.get('siteLocationName')
    if not isinstance(site, str):
        raise ValueError('missing site')
    return {'id': str(bill['sessionId']), 'vin': bill['vin'], 'start': start, 'end': end,
            'amount': amount, 'energy': energy, 'site': site,
            'currency': 'USD', 'pending': pending}


def corroborates(bill, session):
    try:
        return (type(session.get('id')) is int and session.get('vin') == bill['vin']
                and str(session.get('geofence_id')) != '1'
                and abs((timestamp(session.get('start_date'), db=True) - bill['start']).total_seconds()) <= 300
                and abs((timestamp(session.get('end_date'), db=True) - bill['end']).total_seconds()) <= 60
                and abs(number(session.get('charge_energy_used')) - bill['energy'])
                <= max(Decimal('0.25'), bill['energy'] * Decimal('0.02')))
    except (ValueError, TypeError, OverflowError):
        return False


def build_preview(history, sessions):
    rows = []
    records = history_records(history)
    candidate_groups = {}
    for index, raw in enumerate(records):
        row = {'external_id': None, 'session_id': None, 'status': 'invalid', 'amount': None,
               'currency': None, 'site': None, 'started_at': None, 'ended_at': None,
               'reason': None, 'evidence': {}}
        try:
            bill = normalize_bill(raw)
            row.update(external_id=bill['id'], amount=format(bill['amount'], '.2f'),
                       currency=bill['currency'], site=bill['site'],
                       started_at=bill['start'].isoformat().replace('+00:00', 'Z'),
                       ended_at=bill['end'].isoformat().replace('+00:00', 'Z'),
                       evidence={'billed_kwh': str(bill['energy'])})
            matches = [s for s in sessions if corroborates(bill, s)]
            for target in {s['id'] for s in matches}:
                candidate_groups.setdefault(target, []).append(index)
            if bill['pending']:
                row.update(status='pending', reason='Awaiting complete paid invoice',
                           session_id=matches[0]['id'] if len(matches) == 1 else None)
                rows.append(row)
                continue
            if len(matches) != 1:
                row.update(status='ambiguous' if matches else 'unmatched', reason='No unique corroborated session')
            else:
                matched = matches[0]
                row.update(session_id=matched['id'], status='protected' if matched.get('cost') is not None else 'ready',
                           reason='Existing cost protected' if matched.get('cost') is not None else 'VIN, start, end and charger energy corroborated')
        except (ValueError, TypeError, KeyError, ArithmeticError):
            row['reason'] = 'Invalid or unsupported billing record'
        rows.append(row)
    # Preserve IDs even on invalid records, so a duplicate cannot bypass collision checks.
    ids = {}
    for index, (raw, row) in enumerate(zip(records, rows)):
        if isinstance(raw, dict) and type(raw.get('sessionId')) is int:
            row['external_id'] = str(raw['sessionId'])
            ids.setdefault(row['external_id'], []).append(index)
    for group in list(ids.values()) + list(candidate_groups.values()):
        if len(group) > 1:
            for index in group:
                rows[index].update(status='conflict', reason='Duplicate billing ID or shared session target')
    return rows
