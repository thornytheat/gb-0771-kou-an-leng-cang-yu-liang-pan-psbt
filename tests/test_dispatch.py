from app.parcels import load_example
from app.pipeline import review
from app.modules.dispatch.service import evaluate


def test_verdict_priority_frozen_warm_pending_ok():
    stages = review(load_example('dirty'))
    verdict = {row['lot']: row for row in evaluate(stages)['rows']}
    # 收盘在库：F01 开门超限，F03/F05 查验冻结；F02/F04 已出库不在台
    assert set(verdict) == {'F01', 'F03', 'F05'}
    assert verdict['F01']['verdict'] == 'warm'
    assert verdict['F03']['verdict'] == 'frozen'
    assert verdict['F05']['verdict'] == 'frozen'
    assert evaluate(stages)['releasable'] == []


def test_clean_sample_has_releasable_lots():
    verdict = evaluate(review(load_example('clean')))
    by_lot = {row['lot']: row['verdict'] for row in verdict['rows']}
    assert by_lot == {'F01': 'ok', 'F02': 'ok', 'F03': 'frozen'}
    assert verdict['releasable'] == ['F01', 'F02']


def test_open_docket_idempotent(client, archived):
    first = client.post(f"/api/dispatch/{archived['id']}/open").json
    second = client.post(f"/api/dispatch/{archived['id']}/open").json
    assert first['docket']['created_at'] == second['docket']['created_at']
    assert client.get(f"/api/dispatch/{archived['id']}").status_code == 200


def test_frozen_warm_pending_lots_cannot_be_released(client):
    rid = client.post('/api/receipts', json={'sample': 'dirty'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    for lot, code in [('F05', 'HOLD_NOT_RELEASABLE'), ('F03', 'HOLD_NOT_RELEASABLE'),
                      ('F01', 'HOLD_NOT_RELEASABLE')]:
        response = client.post(f'/api/dispatch/{rid}/confirm', json={'lots': [lot]})
        assert (response.status_code, response.json['code']) == (409, code)


def test_confirm_records_decision_and_outbound_together(client):
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    result = client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']}).json
    assert result['reused'] is False and len(result['lines']) == 1 and result['lines'][0]['decision'] == 'release'
    assert result['outbound'] == [{**result['outbound'][0], 'lot': 'F01', 'kind': 'OUT'}]
    assert result['outbound'][0]['event'] == result['order_id']
    board = client.get(f'/api/dispatch/{rid}').json
    assert len(board['orders']) == 1
    assert board['orders'][0]['lines'][0]['lot'] == 'F01'
    assert board['orders'][0]['outbound'][0]['lot'] == 'F01'


def test_retry_same_request_does_not_duplicate_outbound(client):
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    first = client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']}).json
    retry = client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']}).json
    assert retry['reused'] is True and retry['order_id'] == first['order_id']
    board = client.get(f'/api/dispatch/{rid}').json
    assert len(board['orders']) == 1
    assert sum(len(order['outbound']) for order in board['orders']) == 1


def test_mixing_released_lot_is_rejected(client):
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']})
    response = client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01', 'F02']})
    assert (response.status_code, response.json['code']) == (409, 'ALREADY_RELEASED')


def test_unknown_and_malformed_lots_rejected(client):
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    assert client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['ZZZ']}).json['code'] == 'LOT_NOT_PENDING'
    for payload in ({}, {'lots': []}, {'lots': 'F01'}, {'lots': ['F01', 'F01']}, None):
        assert client.post(f'/api/dispatch/{rid}/confirm', json=payload).json['code'] == 'BAD_DOCKET'


def test_reevaluate_keeps_decision_and_flags_released_warm_lot(client):
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']})
    pack = load_example('clean')
    pack['doors'] += ('\nEAST,OPEN,2026-11-21T08:10:00+08:00\nEAST,CLOSED,2026-11-21T08:40:00+08:00'
                      '\nWEST,OPEN,2026-11-21T08:10:00+08:00\nWEST,CLOSED,2026-11-21T08:40:00+08:00')
    pack['temperatures'] += ('\nEAST,6.5,2026-11-21T08:10:00+08:00\nEAST,6.5,2026-11-21T08:30:00+08:00'
                             '\nWEST,7.0,2026-11-21T08:10:00+08:00\nWEST,7.0,2026-11-21T08:30:00+08:00')
    result = client.post(f'/api/dispatch/{rid}/reevaluate', json=pack).json
    assert result['current_id'] != rid and result['flags'] == ['F01']
    rows = {row['lot']: row for row in result['board']['rows']}
    assert rows['F01']['verdict'] == 'released' and rows['F01']['investigate'] is True
    assert rows['F02']['verdict'] == 'warm'  # 待放行货按新证据重判为超温，挡住
    assert client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F02']}).status_code == 409
    # 原放行单与出库不变，未重复出库
    board = client.get(f'/api/dispatch/{rid}').json
    assert len(board['orders']) == 1 and sum(len(o['outbound']) for o in board['orders']) == 1


def test_reevaluate_with_unchanged_pack_keeps_current_archive(client):
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    result = client.post(f'/api/dispatch/{rid}/reevaluate', json={'sample': 'clean'}).json
    assert result['unchanged'] is True and result['current_id'] == rid
    assert client.get(f'/api/receipts').json['count'] == 1


def test_same_archive_new_trace_flag_is_a_change(client):
    # 先在 cool 证据下放行 F01，再补出让 F01 超温的证据重判：首次产生待追查标记（changed）；
    # 随后用同一超温原件再次重判，归档号与标记都不变 -> unchanged 为 True
    cool = load_example('clean')
    warm_pack = load_example('clean')
    warm_pack['doors'] += '\nEAST,OPEN,2026-11-21T08:10:00+08:00\nEAST,CLOSED,2026-11-21T08:40:00+08:00'
    warm_pack['temperatures'] += '\nEAST,6.5,2026-11-21T08:10:00+08:00\nEAST,6.5,2026-11-21T08:30:00+08:00'
    rid = client.post('/api/receipts', json=cool).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']})
    first = client.post(f'/api/dispatch/{rid}/reevaluate', json=warm_pack).json
    assert first['unchanged'] is False and first['flags'] == ['F01']
    second = client.post(f'/api/dispatch/{rid}/reevaluate', json=warm_pack).json
    assert second['unchanged'] is True and second['flags'] == ['F01']
    assert client.get('/api/receipts').json['count'] == 2


def test_late_temperature_rescues_pending_lot(client):
    pack = load_example('clean')
    pack['doors'] += '\nEAST,OPEN,2026-11-21T08:30:00+08:00\nEAST,CLOSED,2026-11-21T08:40:00+08:00'
    rid = client.post('/api/receipts', json=pack).json['id']
    board = client.post(f'/api/dispatch/{rid}/open').json
    assert next(row for row in board['rows'] if row['lot'] == 'F01')['verdict'] == 'pending'
    assert client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']}).status_code == 409
    pack['temperatures'] += '\nEAST,3.0,2026-11-21T08:30:00+08:00'
    result = client.post(f'/api/dispatch/{rid}/reevaluate', json=pack).json
    assert next(row for row in result['board']['rows'] if row['lot'] == 'F01')['verdict'] == 'ok'
    assert result['flags'] == []
    assert client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']}).status_code == 200


def test_dispatch_page_requires_archive(client):
    assert client.get('/view/dispatch').status_code == 404


def test_dispatch_page_renders_board(client):
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    html = client.get(f'/view/dispatch?receipt={rid}').get_data(as_text=True)
    assert '放行核对' in html and '确认放行并出库留档' in html and 'data-receipt="' in html


def test_docket_decided_time_persisted(client):
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    result = client.post(f'/api/dispatch/{rid}/confirm', json={'lots': ['F01']}).json
    docket = client.get(f'/api/dispatch/{rid}').json['docket']
    assert docket['decided_at'] == result['decided_at']


def test_concurrent_confirm_creates_single_order(client):
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.dispatch.service import confirm
    rid = client.post('/api/receipts', json={'sample': 'clean'}).json['id']
    client.post(f'/api/dispatch/{rid}/open')
    db = client.application.extensions['ice_receipts']
    dockets_store = client.application.extensions['ice_dockets']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: confirm(db, dockets_store, rid, ['F01']), range(4)))
    assert len({item['order_id'] for item in results}) == 1
    board = client.get(f'/api/dispatch/{rid}').json
    assert len(board['orders']) == 1
    assert sum(len(order['outbound']) for order in board['orders']) == 1
