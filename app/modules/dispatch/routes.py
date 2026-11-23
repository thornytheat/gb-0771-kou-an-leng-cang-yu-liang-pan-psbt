from flask import Blueprint, current_app, jsonify, request
from app.faults import DockFault
from app.parcels import receive_pack
from .service import confirm, open_docket, reevaluate, board_view

dispatch = Blueprint('dispatch', __name__, url_prefix='/api/dispatch')


def archive():
    return current_app.extensions['ice_receipts']


def dockets():
    return current_app.extensions['ice_dockets']


@dispatch.post('/<receipt_id>/open')
def open_board(receipt_id):
    docket, document = open_docket(archive(), dockets(), receipt_id)
    return jsonify(board_view(document, docket))


@dispatch.get('/<receipt_id>')
def board(receipt_id):
    docket = dockets().get(receipt_id)
    return jsonify(board_view(archive().read(docket['current_id']), docket))


@dispatch.post('/<receipt_id>/confirm')
def release(receipt_id):
    body = request.get_json(silent=True)
    lots = body.get('lots') if isinstance(body, dict) else None
    return jsonify(confirm(archive(), dockets(), receipt_id, lots))


@dispatch.post('/<receipt_id>/reevaluate')
def reopen(receipt_id):
    return jsonify(reevaluate(archive(), dockets(), receipt_id,
                              receive_pack(request.get_json(silent=True))))
