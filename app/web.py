from flask import Blueprint, current_app, jsonify, render_template, request
from .faults import DockFault
from .labels import TABS, CODES, KINDS, DISPATCH_VERDICTS
from .modules.certificate.service import issue
from .modules.dispatch.service import board_view, open_docket
from .modules.history.routes import receipts
from .parcels import load_example, receive_pack
from .pipeline import review
from .swimlanes import lanes, minutes
from .utc import iso

board = Blueprint('board', __name__)


def dockets():
    return current_app.extensions['ice_dockets']


def selection():
    key, example = request.args.get('receipt'), request.args.get('sample', 'dirty')
    if key:
        doc = receipts().read(key)
        return doc['stages'], doc['manifest'], example
    return review(load_example(example)), None, example


@board.get('/api/observe/<view>')
def observe(view):
    if view not in tuple(TABS)[:5]:
        raise DockFault('NO_VIEW', '未找到查验入口', 404)
    key = request.args.get('receipt')
    if key:
        return jsonify(receipts().read(key)['stages'][view])
    return jsonify(review(load_example(request.args.get('sample', 'dirty')), until=view))


@board.post('/api/observe/<view>')
def inspect_originals(view):
    if view not in tuple(TABS)[:5]:
        raise DockFault('NO_VIEW', '未找到查验入口', 404)
    return jsonify(review(receive_pack(request.get_json(silent=True)), until=view))


@board.get('/', defaults={'view':'exposure'})
@board.get('/view/<view>')
def page(view):
    if view not in TABS:
        raise DockFault('NO_VIEW', '未找到查验入口', 404)
    stages, stamp, example = selection()
    if view == 'certificate' and stamp:
        issue(receipts().read(stamp['id']))
    tracks, ticks = lanes(stages)
    dispatch_board = None
    if view == 'dispatch':
        if not stamp:
            raise DockFault('NO_RECEIPT', '先归档本班原件再开台放行', 404)
        docket, document = open_docket(receipts(), dockets(), stamp['id'])
        dispatch_board = board_view(document if docket['current_id'] == stamp['id']
                                    else receipts().read(docket['current_id']), docket)
    return render_template('station.html', stages=stages, view=view, stamp=stamp, sample=example, tabs=TABS,
                           codes=CODES, kinds=KINDS, dispatch_verdicts=DISPATCH_VERDICTS,
                           dispatch_board=dispatch_board,
                           tracks=tracks, ticks=ticks, utc=iso, minutes=minutes,
                           entries=receipts().list() if view == 'history' else [])
