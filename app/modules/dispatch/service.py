from collections import Counter
from datetime import datetime, timezone
from app.faults import DockFault
from app.modules.history.service import save
from app.pipeline import review
from app.seals import compact, sha
from .storage import Dockets

# 判定优先级：冻结 > 已确认超温 > 待补证 > 可放行
VERDICTS = ('ok', 'pending', 'warm', 'frozen')
REASONS = {
    'ok': '证据齐全可装车',
    'frozen': '查验冻结不得放行',
    'warm': '开门超限已确认',
    'missing_temp': '温度记录待补',
    'missing_slot': '槽位证据待补',
    'missing_door': '门态记录待补',
}
BLOCKING = ('frozen', 'warm', 'pending')


def _lot_exposure(stages):
    """汇总每个货批的暴露分类秒数。"""
    by_lot = {}
    for row in stages['exposure']['rows']:
        by_lot.setdefault(row['lot'], Counter())[row['kind']] += row['seconds']
    return by_lot


def evaluate(stages):
    """按收盘在库货批逐批给出放行判定，已出库货批不在待放行台。"""
    exposure = _lot_exposure(stages)
    rows = []
    for lot, state in sorted(stages['custody']['states'].items()):
        pieces = stages['registry']['lots'][lot]['pieces']
        seconds = exposure.get(lot, Counter())
        frozen = bool(state['ticket'])
        missing = next((kind for kind in ('missing_slot', 'missing_door', 'missing_temp')
                        if seconds.get(kind)), None)
        if frozen:
            verdict, reason = 'frozen', REASONS['frozen']
        elif seconds.get('warm'):
            verdict, reason = 'warm', REASONS['warm']
        elif missing:
            verdict, reason = 'pending', REASONS[missing]
        else:
            verdict, reason = 'ok', REASONS['ok']
        rows.append(dict(lot=lot, pieces=pieces, slot=state['slot'], ticket=state['ticket'],
                         verdict=verdict, reason=reason, frozen=frozen,
                         warm_seconds=seconds.get('warm', 0),
                         missing_temp=seconds.get('missing_temp', 0),
                         missing_slot=seconds.get('missing_slot', 0),
                         missing_door=seconds.get('missing_door', 0)))
    counts = Counter(row['verdict'] for row in rows)
    return dict(rows=rows, counts={key: counts.get(key, 0) for key in VERDICTS},
                releasable=[row['lot'] for row in rows if row['verdict'] == 'ok'])


def _now():
    return datetime.now(timezone.utc).isoformat()


def open_docket(archive, dockets: Dockets, receipt_id):
    document = archive.read(receipt_id)
    return dockets.open(receipt_id, receipt_id, _now()), document


def board_view(document, docket):
    """把判定结果与已落库决定合并成放行台视图（document 取 current_id 对应最新归档）。"""
    verdict = evaluate(document['stages'])
    released = {line['lot']: line for order in docket['orders'] for line in order['lines']}
    for row in verdict['rows']:
        prior = released.get(row['lot'])
        row['released'] = prior is not None
        if prior:
            row['decision'] = prior['decision']
            row['order_id'] = prior['order_id']
            row['decided_at'] = prior['decided_at']
            row['investigate'] = row['lot'] in docket['flags']
            row['current_verdict'] = row['verdict']
            row['verdict'] = 'released'
            row['reason'] = '已离库 · 保留原决定' + (' · 补出超温待追查' if row['investigate'] else '')
    return dict(docket=dict(receipt_id=docket['receipt_id'], current_id=docket['current_id'],
                            flags=docket['flags'], decided_at=docket['decided_at'],
                            created_at=docket['created_at']),
                rows=verdict['rows'], counts=verdict['counts'],
                releasable=verdict['releasable'],
                orders=[dict(id=order['id'], decided_at=order['decided_at'],
                             lines=order['lines'], outbound=order['outbound'])
                        for order in docket['orders']])


def confirm(archive, dockets: Dockets, receipt_id, lots):
    """确认放行：校验全部可放，决定与出库一次留档，重试不重复出库。"""
    docket = dockets.get(receipt_id)
    document = archive.read(docket['current_id'])
    if not isinstance(lots, list) or not lots or not all(isinstance(lot, str) for lot in lots):
        raise DockFault('BAD_DOCKET', '提交待放行的货批清单')
    if len(lots) != len(set(lots)):
        raise DockFault('BAD_DOCKET', '货批重复提交')
    already = {line['lot']: order for order in docket['orders'] for line in order['lines']}
    touched = [lot for lot in lots if lot in already]
    if touched:
        # 整单重试：请求与既有放行单完全一致时幂等返回，不重复出库；夹带已离库货批则拒绝
        orders = {already[lot]['id'] for lot in touched}
        if len(orders) == 1 and len(touched) == len(lots):
            existing = next(order for order in docket['orders'] if order['id'] in orders)
            if {line['lot'] for line in existing['lines']} == set(lots):
                return dict(order_id=existing['id'], reused=True, decided_at=existing['decided_at'],
                            lines=existing['lines'], outbound=existing['outbound'],
                            board=board_view(document, docket))
        raise DockFault('ALREADY_RELEASED', '货批已离库，原决定保留', 409)
    verdict = {row['lot']: row for row in evaluate(document['stages'])['rows']}
    unknown = [lot for lot in lots if lot not in verdict]
    if unknown:
        raise DockFault('LOT_NOT_PENDING', f"货批 {','.join(unknown)} 不在待放行在库清单", 409)
    blocked = [(lot, verdict[lot]['reason']) for lot in lots if verdict[lot]['verdict'] in BLOCKING]
    if blocked:
        raise DockFault('HOLD_NOT_RELEASABLE',
                        '存在冻结、超温或待补证货批：' + '；'.join(f"{lot} {why}" for lot, why in blocked), 409)
    decided_at = _now()
    lines = [dict(lot=lot, decision='release', pieces=verdict[lot]['pieces'],
                  slot=verdict[lot]['slot'], verdict='ok', reason=verdict[lot]['reason'],
                  decided_at=decided_at) for lot in lots]
    outbound = [dict(lot=lot, kind='OUT', slot='', ticket='',
                     effective=decided_at, received=decided_at,
                     source='dispatch', decision='release') for lot in lots]
    # 内容寻址单号：同一放行台同一批货的重试（含并发）得到相同单号，靠 INSERT OR IGNORE 只出库一次
    order_id = 'R' + sha(compact([receipt_id, lots]))[:16]
    for line in lines:
        line['order_id'] = order_id
    for move in outbound:
        move['event'] = order_id
    # 确定性单号 + INSERT OR IGNORE：并发或重试都只产生一张放行单、一次出库
    created = dockets.save_order(order_id, receipt_id, decided_at, lines, outbound)
    existing = dockets.order(order_id)
    docket = dockets.get(receipt_id)
    return dict(order_id=order_id, reused=not created, decided_at=existing['decided_at'],
                lines=existing['lines'], outbound=existing['outbound'],
                board=board_view(document, docket))


def reevaluate(archive, dockets: Dockets, receipt_id, files):
    """迟到证据补齐后复算：待放行重新判定，已离库保留原决定，补出超温标待追查。"""
    docket = dockets.get(receipt_id)
    verdict = {row['lot']: row for row in evaluate(review(files))['rows']}
    released = {line['lot'] for order in docket['orders'] for line in order['lines']}
    flags = list(docket['flags'])
    for lot in released:
        row = verdict.get(lot)
        if row and row['verdict'] == 'warm' and lot not in flags:
            flags.append(lot)
    # 新原件单独归档作为本次重判依据；原件未变则复用当前归档，原档案与原决定始终不动
    refreshed = save(archive, files)
    current_id = refreshed['id']
    unchanged = current_id == docket['current_id'] and flags == docket['flags']
    if not unchanged:
        docket = dockets.reevaluate(receipt_id, current_id, flags)
    return dict(receipt_id=receipt_id, current_id=current_id, unchanged=unchanged,
                flags=flags, board=board_view(archive.read(current_id), docket))
